from __future__ import annotations

from orchestrator.workloads.base import (
    CAP_EXEC_INJECT,
    CAP_HTTP_PREDICT,
    CAP_MODEL_MOUNT,
    CAP_READY_PROBE,
    WorkloadProfile,
)
from orchestrator.workloads.hooks import ExecInjectHook, HttpPredictHook, HttpReadyHook, ModelMountHook


WORKLOAD = WorkloadProfile(
    name="tf-serving-half-plus-two",
    description="TensorFlow Serving half_plus_two demo profile.",
    ready_hook=HttpReadyHook("http://127.0.0.1:8501/v1/models/half_plus_two"),
    predict_hook=HttpPredictHook(
        "http://127.0.0.1:8501/v1/models/half_plus_two:predict",
        '{"instances":[1.0,2.0,1.5]}',
    ),
    inject_hook=ExecInjectHook("""read_file() {
  local f="$1"
  if [ -f "$f" ] && [ -r "$f" ]; then
    dd if="$f" of=/dev/null bs=1M count=8 status=none 2>/dev/null || head -c 131072 "$f" >/dev/null 2>&1
  fi
}
read_model_payload() {
  local version_dir="$1"
  local mf=""
  for mf in \
    "$version_dir/saved_model.pb" \
    "$version_dir/saved_model.pbtxt" \
    "$version_dir/fingerprint.pb"; do
    read_file "$mf"
  done
  for subdir in variables assets assets.extra; do
    if [ -d "$version_dir/$subdir" ]; then
      find "$version_dir/$subdir" -maxdepth 1 -type f -print 2>/dev/null \
        | sort -u \
        | head -n 16 \
        | while read -r mf; do
            read_file "$mf"
          done
    fi
  done
}
for f in \
  /etc/ld.so.cache \
  /etc/os-release \
  /etc/nsswitch.conf \
  /etc/passwd \
  /lib/x86_64-linux-gnu/libc.so.6 \
  /lib/x86_64-linux-gnu/libdl.so.2 \
  /lib/x86_64-linux-gnu/libpthread.so.0 \
  /lib/x86_64-linux-gnu/libselinux.so.1 \
  /lib/x86_64-linux-gnu/libpcre2-8.so.0 \
  /usr/lib/x86_64-linux-gnu/libnss_files.so.2; do
  read_file "$f"
done
if [ -d /models/half_plus_two ]; then
  for version_dir in /models/half_plus_two/[0-9]*; do
    if [ -d "$version_dir" ]; then
      read_model_payload "$version_dir"
    fi
  done
fi"""),
    model_mount_hook=ModelMountHook("/models/half_plus_two", "half_plus_two", "/models"),
    sample_ready_timeout=30,
    benchmark_timeout=180,
    capabilities=(
        CAP_READY_PROBE,
        CAP_HTTP_PREDICT,
        CAP_EXEC_INJECT,
        CAP_MODEL_MOUNT,
    ),
)
