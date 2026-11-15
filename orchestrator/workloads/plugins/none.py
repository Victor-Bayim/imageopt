from __future__ import annotations

from orchestrator.workloads.base import WorkloadProfile


WORKLOAD = WorkloadProfile(
    name="none",
    description="No implicit workload defaults; all runtime probes and benchmark endpoints must be set explicitly.",
    capabilities=(),
)
