from __future__ import annotations

from pathlib import Path

from orchestrator.workloads.base import CAP_HTTP_PREDICT, CAP_READY_PROBE, WorkloadProfile
from orchestrator.workloads.hooks import HttpPredictHook, HttpReadyHook


_SAMPLE_INPUT = (
    Path(__file__).resolve().parents[3]
    / "ready-serving-layout-trap-v5"
    / "probes"
    / "sample_input.json"
).read_text(encoding="utf-8").strip()


WORKLOAD = WorkloadProfile(
    name="ready-serving-layout-trap-v5",
    description="Self-starting layout-trap v5 ready-serving service profile.",
    self_starting_service=True,
    ready_hook=HttpReadyHook("http://127.0.0.1:8000/ready"),
    debug_state_url="http://127.0.0.1:8000/debug/state",
    predict_hook=HttpPredictHook(
        "http://127.0.0.1:8000/predict",
        _SAMPLE_INPUT,
        attempts=3,
    ),
    sample_ready_timeout=120,
    benchmark_timeout=180,
    capabilities=(
        CAP_READY_PROBE,
        CAP_HTTP_PREDICT,
    ),
)
