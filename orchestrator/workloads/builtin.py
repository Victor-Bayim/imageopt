from __future__ import annotations

import importlib
import pkgutil

from orchestrator.workloads.base import WorkloadProfile
from orchestrator.workloads.registry import register_workload


PLUGIN_PACKAGE = "orchestrator.workloads.plugins"


def builtin_workloads() -> tuple[WorkloadProfile, ...]:
    package = importlib.import_module(PLUGIN_PACKAGE)
    profiles: list[WorkloadProfile] = []
    for module_info in sorted(pkgutil.iter_modules(package.__path__), key=lambda item: item.name):
        module = importlib.import_module(f"{PLUGIN_PACKAGE}.{module_info.name}")
        profile = getattr(module, "WORKLOAD", None)
        if not isinstance(profile, WorkloadProfile):
            raise TypeError(f"{PLUGIN_PACKAGE}.{module_info.name} must export WORKLOAD: WorkloadProfile")
        profiles.append(profile)
    return tuple(profiles)


def register_builtin_workloads() -> None:
    for profile in builtin_workloads():
        register_workload(profile, replace=True)
