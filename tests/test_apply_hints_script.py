import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "apply_hints_to_image.sh"


def write_fake_nerdctl(
    bin_dir: Path,
    args_log: Path,
    *,
    fail_on_inspect_ref: str = "",
    fail_on_run_ref: str = "",
) -> None:
    path = bin_dir / "nerdctl"
    path.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env bash
            set -euo pipefail
            printf '%s\\n' "$*" >> "{args_log}"
            if [[ "${{1:-}}" == "--namespace" ]]; then
              shift 2
            fi
            if [[ "${{1:-}}" == "image" && "${{2:-}}" == "inspect" ]]; then
              ref="${{3:-}}"
              if [[ -n "{fail_on_inspect_ref}" && "$ref" == "{fail_on_inspect_ref}" ]]; then
                echo 'image not available: "{fail_on_inspect_ref}"' >&2
                exit 1
              fi
              printf '{{}}\\n'
              exit 0
            fi
            if [[ "${{1:-}}" == "run" ]]; then
              ref="${{@: -1}}"
              if [[ -n "{fail_on_run_ref}" && "$ref" == "{fail_on_run_ref}" ]]; then
                echo 'image not available: "{fail_on_run_ref}"' >&2
                exit 1
              fi
              exit 0
            fi
            if [[ "${{1:-}}" == "images" && "${{2:-}}" == "import" ]]; then
              exit 0
            fi
            exit 0
            """
        ),
        encoding="utf-8",
    )
    path.chmod(0o755)


class ApplyHintsScriptTests(unittest.TestCase):
    def test_stale_prefetch_list_is_regenerated_from_newer_hints(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            work_dir = tmp_path / "work"
            bin_dir = tmp_path / "bin"
            args_log = tmp_path / "ctr_args.log"
            work_dir.mkdir()
            bin_dir.mkdir()

            stale_paths = [f"/app/stale_{index}.so" for index in range(4)]
            fresh_paths = [f"/app/manual_{index:02d}.so" for index in range(21)]
            (work_dir / "estargz.list").write_text(
                "".join(f"{path}\n" for path in stale_paths),
                encoding="utf-8",
            )
            (work_dir / "hints.yaml").write_text(
                "P0:\n" + "".join(f"  - {path}\n" for path in fresh_paths) + "P1: []\nP2: []\nP3: []\n",
                encoding="utf-8",
            )
            (work_dir / "ranked.yaml").write_text(
                "P0:\n" + "".join(f"  - {path}\n" for path in fresh_paths) + "P1: []\nP2: []\nP3: []\n",
                encoding="utf-8",
            )
            (work_dir / "llm_result.json").write_text(
                '{"final_hints_source":"grounded_llm"}\n',
                encoding="utf-8",
            )
            (work_dir / "selector_stats_hints.json").write_text(
                '{"selector_source":"grounded_llm_manual"}\n',
                encoding="utf-8",
            )
            os.utime(work_dir / "estargz.list", (1, 1))
            os.utime(work_dir / "hints.yaml", (2, 2))
            os.utime(work_dir / "ranked.yaml", (3, 3))

            fake_ctr_remote = bin_dir / "ctr-remote"
            fake_ctr_remote.write_text(
                textwrap.dedent(
                    f"""\
                    #!/usr/bin/env bash
                    set -euo pipefail
                    if [[ "${{1:-}}" == "image" && "${{2:-}}" == "optimize" && "${{3:-}}" == "-h" ]]; then
                      cat <<'EOF'
                    OPTIONS:
                       --oci
                       --prefetch-list value
                    EOF
                      exit 0
                    fi
                    printf '%s\\n' "$*" > "{args_log}"
                    exit 0
                    """
                ),
                encoding="utf-8",
            )
            fake_ctr_remote.chmod(0o755)

            env = dict(os.environ)
            env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
            proc = subprocess.run(
                [
                    "bash",
                    str(SCRIPT),
                    "--work",
                    str(work_dir),
                    "--src",
                    "docker.io/library/app:latest",
                    "--dst",
                    "docker.io/library/app:latest-stargz",
                    "--dry-run",
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
                env=env,
            )

            self.assertEqual(0, proc.returncode, msg=proc.stderr or proc.stdout)
            self.assertEqual(fresh_paths, (work_dir / "estargz.list").read_text(encoding="utf-8").splitlines())
            self.assertEqual(fresh_paths, (work_dir / "estargz.list.cleaned").read_text(encoding="utf-8").splitlines())
            self.assertIn("hints source: grounded_llm", proc.stdout)
            self.assertIn("selector source: grounded_llm_manual", proc.stdout)
            self.assertIn(f"input hints path: {work_dir / 'hints.yaml'}", proc.stdout)
            self.assertIn(f"generated prefetch list path: {work_dir / 'estargz.list'}", proc.stdout)
            self.assertIn("stale prefetch list detected: true (reason=hints_newer_than_estargz_list)", proc.stdout)
            self.assertIn(
                f"prefetch list: {work_dir / 'estargz.list.cleaned'} (lines=21, regenerated=true)",
                proc.stdout,
            )

    def test_cleaned_prefetch_list_is_deduplicated_and_filters_special_paths_in_dry_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            work_dir = tmp_path / "work"
            bin_dir = tmp_path / "bin"
            args_log = tmp_path / "ctr_args.log"
            work_dir.mkdir()
            bin_dir.mkdir()

            (work_dir / "estargz.list").write_text(
                "/app/a.so\n"
                "/app/a.so\n"
                "/dev/null\n"
                "/run/demo.sock\n"
                "/var/run/demo.pid\n"
                "relative\n"
                "/app/dir/\n"
                "/app/b.so\n",
                encoding="utf-8",
            )

            fake_ctr_remote = bin_dir / "ctr-remote"
            fake_ctr_remote.write_text(
                textwrap.dedent(
                    f"""\
                    #!/usr/bin/env bash
                    set -euo pipefail
                    if [[ "${{1:-}}" == "image" && "${{2:-}}" == "optimize" && "${{3:-}}" == "-h" ]]; then
                      cat <<'EOF'
                    OPTIONS:
                       --oci
                       --prefetch-list value
                    EOF
                      exit 0
                    fi
                    printf '%s\\n' "$*" > "{args_log}"
                    exit 0
                    """
                ),
                encoding="utf-8",
            )
            fake_ctr_remote.chmod(0o755)

            env = dict(os.environ)
            env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
            proc = subprocess.run(
                [
                    "bash",
                    str(SCRIPT),
                    "--work",
                    str(work_dir),
                    "--src",
                    "docker.io/library/app:latest",
                    "--dst",
                    "docker.io/library/app:latest-stargz",
                    "--dry-run",
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
                env=env,
            )

            self.assertEqual(0, proc.returncode, msg=proc.stderr or proc.stdout)
            cleaned = (work_dir / "estargz.list.cleaned").read_text(encoding="utf-8")
            self.assertEqual("/app/a.so\n/app/b.so\n", cleaned)
            self.assertFalse((work_dir / "optimized_image.txt").exists())
            self.assertIn(str(work_dir / "estargz.list.cleaned"), proc.stdout)

    def test_ctr_remote_flag_detection_does_not_infer_estargz_from_subflags(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            work_dir = tmp_path / "work"
            bin_dir = tmp_path / "bin"
            args_log = tmp_path / "ctr_args.log"
            nerdctl_log = tmp_path / "nerdctl_args.log"
            work_dir.mkdir()
            bin_dir.mkdir()

            (work_dir / "estargz.list").write_text(
                "/app/a.so\n/dev/null\nrelative\n",
                encoding="utf-8",
            )

            fake_ctr_remote = bin_dir / "ctr-remote"
            fake_ctr_remote.write_text(
                textwrap.dedent(
                    f"""\
                    #!/usr/bin/env bash
                    set -euo pipefail
                    if [[ "${{1:-}}" == "image" && "${{2:-}}" == "optimize" && "${{3:-}}" == "-h" ]]; then
                      cat <<'EOF'
                    NAME:
                       ctr images optimize - optimize an image with user-specified workload

                    OPTIONS:
                       --oci                                    convert Docker media types to OCI media types
                       --estargz-compression-level value        eStargz compression level
                       --prefetch-list value                    path to a text file listing files/patterns to prefetch
                    EOF
                      exit 0
                    fi
                    printf '%s\\n' "$*" > "{args_log}"
                    exit 0
                    """
                ),
                encoding="utf-8",
            )
            fake_ctr_remote.chmod(0o755)
            write_fake_nerdctl(bin_dir, nerdctl_log)

            env = dict(os.environ)
            env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
            proc = subprocess.run(
                [
                    "bash",
                    str(SCRIPT),
                    "--work",
                    str(work_dir),
                    "--src",
                    "docker.io/library/app:latest",
                    "--dst",
                    "docker.io/library/app:latest-stargz",
                    "--ns",
                    "default",
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
                env=env,
            )

            self.assertEqual(0, proc.returncode, msg=proc.stderr or proc.stdout)
            argv = args_log.read_text(encoding="utf-8")
            self.assertIn("--oci", argv)
            self.assertIn("--prefetch-list", argv)
            self.assertNotIn("--estargz", argv)
            self.assertTrue((work_dir / "optimized_image.txt").exists())
            nerdctl_args = nerdctl_log.read_text(encoding="utf-8")
            self.assertIn(
                "--namespace default image inspect docker.io/library/app:latest-stargz",
                nerdctl_args,
            )
            self.assertIn(
                "--namespace default run --rm --pull=never --snapshotter stargz --platform linux/amd64 --entrypoint /bin/true docker.io/library/app:latest-stargz",
                nerdctl_args,
            )

    def test_ctr_remote_path_fails_fast_when_stargz_smoke_check_cannot_consume_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            work_dir = tmp_path / "work"
            bin_dir = tmp_path / "bin"
            args_log = tmp_path / "ctr_args.log"
            nerdctl_log = tmp_path / "nerdctl_args.log"
            work_dir.mkdir()
            bin_dir.mkdir()

            (work_dir / "estargz.list").write_text("/app/a.so\n", encoding="utf-8")

            fake_ctr_remote = bin_dir / "ctr-remote"
            fake_ctr_remote.write_text(
                textwrap.dedent(
                    f"""\
                    #!/usr/bin/env bash
                    set -euo pipefail
                    if [[ "${{1:-}}" == "image" && "${{2:-}}" == "optimize" && "${{3:-}}" == "-h" ]]; then
                      cat <<'EOF'
                    OPTIONS:
                       --oci
                       --prefetch-list value
                    EOF
                      exit 0
                    fi
                    printf '%s\\n' "$*" > "{args_log}"
                    exit 0
                    """
                ),
                encoding="utf-8",
            )
            fake_ctr_remote.chmod(0o755)
            write_fake_nerdctl(
                bin_dir,
                nerdctl_log,
                fail_on_run_ref="docker.io/library/app:latest-stargz",
            )

            env = dict(os.environ)
            env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
            proc = subprocess.run(
                [
                    "bash",
                    str(SCRIPT),
                    "--work",
                    str(work_dir),
                    "--src",
                    "docker.io/library/app:latest",
                    "--dst",
                    "docker.io/library/app:latest-stargz",
                    "--ns",
                    "default",
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
                env=env,
            )

            self.assertNotEqual(0, proc.returncode, msg=proc.stderr or proc.stdout)
            self.assertFalse((work_dir / "optimized_image.txt").exists())
            self.assertIn("image optimize 成功，但对 nerdctl + stargz + pull=never 不可消费", proc.stderr)
            self.assertIn(
                "nerdctl --namespace default image inspect docker.io/library/app:latest-stargz",
                proc.stderr,
            )
            self.assertIn(
                "nerdctl --namespace default run --rm --pull=never --snapshotter stargz --platform linux/amd64 --entrypoint /bin/true docker.io/library/app:latest-stargz",
                proc.stderr,
            )

    def test_ctr_remote_path_repairs_stale_stargz_parent_snapshot_and_retries(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            work_dir = tmp_path / "work"
            bin_dir = tmp_path / "bin"
            ctr_log = tmp_path / "ctr_args.log"
            nerdctl_log = tmp_path / "nerdctl_args.log"
            sudo_log = tmp_path / "sudo_args.log"
            work_dir.mkdir()
            bin_dir.mkdir()

            (work_dir / "estargz.list").write_text("/app/a.so\n", encoding="utf-8")

            fake_ctr_remote = bin_dir / "ctr-remote"
            fake_ctr_remote.write_text(
                textwrap.dedent(
                    f"""\
                    #!/usr/bin/env bash
                    set -euo pipefail
                    printf '%s\\n' "$*" >> "{ctr_log}"
                    if [[ "${{1:-}}" == "image" && "${{2:-}}" == "optimize" && "${{3:-}}" == "-h" ]]; then
                      cat <<'EOF'
                    OPTIONS:
                       --oci
                       --prefetch-list value
                    EOF
                      exit 0
                    fi
                    if [[ "${{1:-}}" == "--namespace" ]]; then
                      shift 2
                    elif [[ "${{1:-}}" == "-n" ]]; then
                      shift 2
                    fi
                    if [[ "${{1:-}}" == "image" && "${{2:-}}" == "optimize" ]]; then
                      exit 0
                    fi
                    if [[ "${{1:-}}" == "snapshots" && "${{2:-}}" == "--snapshotter" && "${{3:-}}" == "stargz" && "${{4:-}}" == "ls" ]]; then
                      cat <<'EOF'
                    KEY PARENT KIND
                    18 - active
                    EOF
                      exit 0
                    fi
                    if [[ "${{1:-}}" == "snapshots" && "${{2:-}}" == "--snapshotter" && "${{3:-}}" == "stargz" && "${{4:-}}" == "rm" && "${{5:-}}" == "18" ]]; then
                      exit 0
                    fi
                    exit 0
                    """
                ),
                encoding="utf-8",
            )
            fake_ctr_remote.chmod(0o755)

            fake_nerdctl = bin_dir / "nerdctl"
            fake_nerdctl.write_text(
                textwrap.dedent(
                    f"""\
                    #!/usr/bin/env bash
                    set -euo pipefail
                    state_file="{tmp_path / 'nerdctl_run_seen'}"
                    printf '%s\\n' "$*" >> "{nerdctl_log}"
                    if [[ "${{1:-}}" == "--namespace" ]]; then
                      shift 2
                    fi
                    if [[ "${{1:-}}" == "image" && "${{2:-}}" == "inspect" ]]; then
                      printf '{{}}\\n'
                      exit 0
                    fi
                    if [[ "${{1:-}}" == "run" ]]; then
                      ref="${{@: -1}}"
                      if [[ "$ref" == "docker.io/library/app:latest-stargz" && ! -f "$state_file" ]]; then
                        touch "$state_file"
                        echo 'time="2026-04-11T14:33:20Z" level=fatal msg="failed to stat parent: stat /var/lib/containerd-stargz-grpc/snapshotter/snapshots/18/fs: no such file or directory"' >&2
                        exit 1
                      fi
                      exit 0
                    fi
                    exit 0
                    """
                ),
                encoding="utf-8",
            )
            fake_nerdctl.chmod(0o755)

            fake_sudo = bin_dir / "sudo"
            fake_sudo.write_text(
                textwrap.dedent(
                    f"""\
                    #!/usr/bin/env bash
                    set -euo pipefail
                    printf '%s\\n' "$*" >> "{sudo_log}"
                    exit 0
                    """
                ),
                encoding="utf-8",
            )
            fake_sudo.chmod(0o755)

            env = dict(os.environ)
            env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
            proc = subprocess.run(
                [
                    "bash",
                    str(SCRIPT),
                    "--work",
                    str(work_dir),
                    "--src",
                    "docker.io/library/app:latest",
                    "--dst",
                    "docker.io/library/app:latest-stargz",
                    "--ns",
                    "default",
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
                env=env,
            )

            self.assertEqual(0, proc.returncode, msg=proc.stderr or proc.stdout)
            self.assertEqual(
                "docker.io/library/app:latest-stargz",
                (work_dir / "optimized_image.txt").read_text(encoding="utf-8").strip(),
            )
            self.assertIn(
                "检测到 stale stargz parent snapshot；尝试清理 namespace=default 的 stargz snapshot 记录并重建本地目录骨架",
                proc.stdout,
            )
            ctr_args = ctr_log.read_text(encoding="utf-8")
            self.assertIn("-n default snapshots --snapshotter stargz ls", ctr_args)
            self.assertIn("-n default snapshots --snapshotter stargz rm 18", ctr_args)
            nerdctl_args = nerdctl_log.read_text(encoding="utf-8")
            self.assertEqual(
                2,
                nerdctl_args.count(
                    "--namespace default run --rm --pull=never --snapshotter stargz --platform linux/amd64 --entrypoint /bin/true docker.io/library/app:latest-stargz"
                ),
            )
            sudo_args = sudo_log.read_text(encoding="utf-8")
            self.assertIn(
                "mkdir -p /var/lib/containerd-stargz-grpc/snapshotter/snapshots",
                sudo_args,
            )
            self.assertIn(
                "rm -rf /var/lib/containerd-stargz-grpc/snapshotter/cache/*",
                sudo_args,
            )

    def test_fallback_toolchain_is_selected_when_ctr_remote_prefetch_support_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            work_dir = tmp_path / "work"
            bin_dir = tmp_path / "bin"
            work_dir.mkdir()
            bin_dir.mkdir()

            (work_dir / "estargz.list").write_text("/app/a.so\n", encoding="utf-8")

            fake_ctr_remote = bin_dir / "ctr-remote"
            fake_ctr_remote.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env bash
                    set -euo pipefail
                    if [[ "${1:-}" == "image" && "${2:-}" == "optimize" && "${3:-}" == "-h" ]]; then
                      cat <<'EOF'
                    OPTIONS:
                       --oci
                    EOF
                      exit 0
                    fi
                    exit 2
                    """
                ),
                encoding="utf-8",
            )
            fake_ctr_remote.chmod(0o755)

            for name in ("estargzify", "nerdctl"):
                path = bin_dir / name
                path.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
                path.chmod(0o755)

            env = dict(os.environ)
            env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
            proc = subprocess.run(
                [
                    "bash",
                    str(SCRIPT),
                    "--work",
                    str(work_dir),
                    "--src",
                    "docker.io/library/app:latest",
                    "--dst",
                    "docker.io/library/app:latest-stargz",
                    "--dry-run",
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
                env=env,
            )

            self.assertEqual(0, proc.returncode, msg=proc.stderr or proc.stdout)
            self.assertIn("estargzify", proc.stdout)
            self.assertIn("images import", proc.stdout)

    def test_ctr_remote_path_canonicalizes_dockerhub_shorthand_refs(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            work_dir = tmp_path / "work"
            bin_dir = tmp_path / "bin"
            args_log = tmp_path / "ctr_args.log"
            nerdctl_log = tmp_path / "nerdctl_args.log"
            work_dir.mkdir()
            bin_dir.mkdir()

            (work_dir / "estargz.list").write_text("/app/a.so\n", encoding="utf-8")

            fake_ctr_remote = bin_dir / "ctr-remote"
            fake_ctr_remote.write_text(
                textwrap.dedent(
                    f"""\
                    #!/usr/bin/env bash
                    set -euo pipefail
                    if [[ "${{1:-}}" == "image" && "${{2:-}}" == "optimize" && "${{3:-}}" == "-h" ]]; then
                      cat <<'EOF'
                    OPTIONS:
                       --oci
                       --prefetch-list value
                    EOF
                      exit 0
                    fi
                    printf '%s\\n' "$*" > "{args_log}"
                    exit 0
                    """
                ),
                encoding="utf-8",
            )
            fake_ctr_remote.chmod(0o755)
            write_fake_nerdctl(bin_dir, nerdctl_log)

            env = dict(os.environ)
            env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
            proc = subprocess.run(
                [
                    "bash",
                    str(SCRIPT),
                    "--work",
                    str(work_dir),
                    "--src",
                    "vllm/vllm-openai:latest",
                    "--dst",
                    "vllm/vllm-openai:latest-stargz",
                    "--ns",
                    "default",
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
                env=env,
            )

            self.assertEqual(0, proc.returncode, msg=proc.stderr or proc.stdout)
            argv = args_log.read_text(encoding="utf-8")
            self.assertIn("docker.io/vllm/vllm-openai:latest", argv)
            self.assertIn("docker.io/vllm/vllm-openai:latest-stargz", argv)
            self.assertEqual(
                "docker.io/vllm/vllm-openai:latest-stargz",
                (work_dir / "optimized_image.txt").read_text(encoding="utf-8").strip(),
            )
            self.assertIn(
                "--namespace default run --rm --pull=never --snapshotter stargz --platform linux/amd64 --entrypoint /bin/true docker.io/vllm/vllm-openai:latest-stargz",
                nerdctl_log.read_text(encoding="utf-8"),
            )

    def test_ctr_remote_path_keeps_oci_for_local_registry_refs_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            work_dir = tmp_path / "work"
            bin_dir = tmp_path / "bin"
            args_log = tmp_path / "ctr_args.log"
            nerdctl_log = tmp_path / "nerdctl_args.log"
            work_dir.mkdir()
            bin_dir.mkdir()

            (work_dir / "estargz.list").write_text("/app/a.so\n", encoding="utf-8")

            fake_ctr_remote = bin_dir / "ctr-remote"
            fake_ctr_remote.write_text(
                textwrap.dedent(
                    f"""\
                    #!/usr/bin/env bash
                    set -euo pipefail
                    if [[ "${{1:-}}" == "image" && "${{2:-}}" == "optimize" && "${{3:-}}" == "-h" ]]; then
                      cat <<'EOF'
                    OPTIONS:
                       --oci
                       --prefetch-list value
                    EOF
                      exit 0
                    fi
                    printf '%s\\n' "$*" > "{args_log}"
                    exit 0
                    """
                ),
                encoding="utf-8",
            )
            fake_ctr_remote.chmod(0o755)
            write_fake_nerdctl(bin_dir, nerdctl_log)

            env = dict(os.environ)
            env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
            proc = subprocess.run(
                [
                    "bash",
                    str(SCRIPT),
                    "--work",
                    str(work_dir),
                    "--src",
                    "127.0.0.1:5002/abtest/ready-serving-layout-trap-v5:src-raw",
                    "--dst",
                    "127.0.0.1:5002/abtest/ready-serving-layout-trap-v5:esgz-test",
                    "--ns",
                    "default",
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
                env=env,
            )

            self.assertEqual(0, proc.returncode, msg=proc.stderr or proc.stdout)
            argv = args_log.read_text(encoding="utf-8")
            self.assertIn("--prefetch-list", argv)
            self.assertIn("--oci", argv)
            self.assertIn(
                "检测到本地 registry 引用；默认保留 ctr-remote --oci，以保留 eStargz TOC annotations",
                proc.stdout,
            )
            self.assertEqual(
                "127.0.0.1:5002/abtest/ready-serving-layout-trap-v5:esgz-test",
                (work_dir / "optimized_image.txt").read_text(encoding="utf-8").strip(),
            )

    def test_ctr_remote_path_disables_oci_for_local_registry_refs_only_with_explicit_opt_out(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            work_dir = tmp_path / "work"
            bin_dir = tmp_path / "bin"
            args_log = tmp_path / "ctr_args.log"
            nerdctl_log = tmp_path / "nerdctl_args.log"
            work_dir.mkdir()
            bin_dir.mkdir()

            (work_dir / "estargz.list").write_text("/app/a.so\n", encoding="utf-8")

            fake_ctr_remote = bin_dir / "ctr-remote"
            fake_ctr_remote.write_text(
                textwrap.dedent(
                    f"""\
                    #!/usr/bin/env bash
                    set -euo pipefail
                    if [[ "${{1:-}}" == "image" && "${{2:-}}" == "optimize" && "${{3:-}}" == "-h" ]]; then
                      cat <<'EOF'
                    OPTIONS:
                       --oci
                       --prefetch-list value
                    EOF
                      exit 0
                    fi
                    printf '%s\\n' "$*" > "{args_log}"
                    exit 0
                    """
                ),
                encoding="utf-8",
            )
            fake_ctr_remote.chmod(0o755)
            write_fake_nerdctl(bin_dir, nerdctl_log)

            env = dict(os.environ)
            env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
            env["IMAGEOPT_DISABLE_OCI_FOR_LOCAL_REGISTRY"] = "1"
            proc = subprocess.run(
                [
                    "bash",
                    str(SCRIPT),
                    "--work",
                    str(work_dir),
                    "--src",
                    "127.0.0.1:5002/abtest/ready-serving-layout-trap-v5:src-raw",
                    "--dst",
                    "127.0.0.1:5002/abtest/ready-serving-layout-trap-v5:esgz-test-optout",
                    "--ns",
                    "default",
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
                env=env,
            )

            self.assertEqual(0, proc.returncode, msg=proc.stderr or proc.stdout)
            argv = args_log.read_text(encoding="utf-8")
            self.assertIn("--prefetch-list", argv)
            self.assertNotIn("--oci", argv)
            self.assertIn(
                "检测到 IMAGEOPT_DISABLE_OCI_FOR_LOCAL_REGISTRY=1；对本地 registry 引用禁用 ctr-remote --oci（历史兼容模式）",
                proc.stderr,
            )
            self.assertEqual(
                "127.0.0.1:5002/abtest/ready-serving-layout-trap-v5:esgz-test-optout",
                (work_dir / "optimized_image.txt").read_text(encoding="utf-8").strip(),
            )

    def test_chunk_shadow_mode_uses_experimental_prefetch_list_and_mode_specific_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            work_dir = tmp_path / "work"
            bin_dir = tmp_path / "bin"
            args_log = tmp_path / "ctr_args.log"
            nerdctl_log = tmp_path / "nerdctl_args.log"
            work_dir.mkdir()
            bin_dir.mkdir()

            (work_dir / "estargz.list").write_text("/app/default.so\n", encoding="utf-8")
            (work_dir / "estargz_chunk_shadow.list").write_text("/app/exp.so\n", encoding="utf-8")

            fake_ctr_remote = bin_dir / "ctr-remote"
            fake_ctr_remote.write_text(
                textwrap.dedent(
                    f"""\
                    #!/usr/bin/env bash
                    set -euo pipefail
                    if [[ "${{1:-}}" == "image" && "${{2:-}}" == "optimize" && "${{3:-}}" == "-h" ]]; then
                      cat <<'EOF'
                    OPTIONS:
                       --oci
                       --prefetch-list value
                    EOF
                      exit 0
                    fi
                    printf '%s\\n' "$*" > "{args_log}"
                    exit 0
                    """
                ),
                encoding="utf-8",
            )
            fake_ctr_remote.chmod(0o755)
            write_fake_nerdctl(bin_dir, nerdctl_log)

            env = dict(os.environ)
            env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
            proc = subprocess.run(
                [
                    "bash",
                    str(SCRIPT),
                    "--work",
                    str(work_dir),
                    "--selector-mode",
                    "chunk-shadow",
                    "--src",
                    "docker.io/library/app:latest",
                    "--dst",
                    "docker.io/library/app:latest-stargz-chunk-shadow",
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
                env=env,
            )

            self.assertEqual(0, proc.returncode, msg=proc.stderr or proc.stdout)
            self.assertEqual(
                "/app/exp.so\n",
                (work_dir / "estargz_chunk_shadow.list.cleaned").read_text(encoding="utf-8"),
            )
            self.assertFalse((work_dir / "optimized_image.txt").exists())
            self.assertEqual(
                "docker.io/library/app:latest-stargz-chunk-shadow",
                (work_dir / "optimized_image_chunk_shadow.txt").read_text(encoding="utf-8").strip(),
            )
            argv = args_log.read_text(encoding="utf-8")
            self.assertIn(str(work_dir / "estargz_chunk_shadow.list.cleaned"), argv)
            self.assertIn(
                "--namespace default run --rm --pull=never --snapshotter stargz --platform linux/amd64 --entrypoint /bin/true docker.io/library/app:latest-stargz-chunk-shadow",
                nerdctl_log.read_text(encoding="utf-8"),
            )


if __name__ == "__main__":
    unittest.main()
