from __future__ import annotations

from pathlib import Path

from orchestrator.workloads.base import CAP_HTTP_PREDICT, CAP_READY_PROBE, WorkloadProfile
from orchestrator.workloads.hooks import HttpPredictHook, HttpReadyHook


_SAMPLE_INPUT = (Path(__file__).resolve().parents[3] / "probes" / "sample_input.json").read_text(
    encoding="utf-8"
).strip()


WORKLOAD = WorkloadProfile(
    name="ready-serving-bootstrap-v2",
    description="Self-starting ready-serving bootstrap v2 service profile.",
    self_starting_service=True,
    ready_hook=HttpReadyHook("http://127.0.0.1:8000/ready"),
    predict_hook=HttpPredictHook(
        "http://127.0.0.1:8000/predict",
        _SAMPLE_INPUT,
    ),
    sample_ready_timeout=30,
    benchmark_timeout=180,
    capabilities=(
        CAP_READY_PROBE,
        CAP_HTTP_PREDICT,
    ),
)
