from __future__ import annotations

from orchestrator.workloads.base import WorkloadProfile


_WORKLOAD_PROFILES: dict[str, WorkloadProfile] = {}


def register_workload(profile: WorkloadProfile, *, replace: bool = False) -> WorkloadProfile:
    existing = _WORKLOAD_PROFILES.get(profile.name)
    if existing is not None and not replace:
        raise ValueError(f"workload profile already registered: {profile.name}")
    _WORKLOAD_PROFILES[profile.name] = profile
    return profile


def workload_choices() -> tuple[str, ...]:
    return tuple(_WORKLOAD_PROFILES.keys())


def get_workload_profile(name: str) -> WorkloadProfile:
    try:
        return _WORKLOAD_PROFILES[name]
    except KeyError as exc:
        raise ValueError(f"unknown workload profile: {name}") from exc
