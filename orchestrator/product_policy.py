from __future__ import annotations


DISTROLESS_GATED_STARTUP_SUPPORTED = "via_host_launcher_static_default_or_dynamic_experimental"
GATED_STARTUP_BACKENDS = [
    "image_shell",
    "host_launcher_static",
    "host_launcher_dynamic_experimental",
]
GATED_STARTUP_BACKEND_ORDER_AUTO = [
    "host_launcher_static",
    "image_shell",
    "host_launcher_dynamic_experimental",
]


def enforced_supports_payload() -> dict[str, object]:
    return {
        "linux_single_host_only": True,
        "runtime": "containerd+nerdctl",
        "privilege": "root-or-passwordless-sudo",
        "distroless_gated_startup_supported": DISTROLESS_GATED_STARTUP_SUPPORTED,
        "gated_startup_backends": list(GATED_STARTUP_BACKENDS),
        "gated_startup_backend_order_auto": list(GATED_STARTUP_BACKEND_ORDER_AUTO),
        "llm_default_enabled": False,
        "source_of_truth": "file_sink",
    }
