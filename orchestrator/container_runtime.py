from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ContainerRuntimeSpec:
    gpus: str = ""
    ipc: str = ""
    entrypoint: str = ""
    env: tuple[str, ...] = ()
    mounts: tuple[str, ...] = ()
    command: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "gpus", (self.gpus or "").strip())
        object.__setattr__(self, "ipc", (self.ipc or "").strip())
        object.__setattr__(self, "entrypoint", (self.entrypoint or "").strip())
        object.__setattr__(
            self,
            "env",
            tuple(item.strip() for item in self.env if item and item.strip()),
        )
        object.__setattr__(
            self,
            "mounts",
            tuple(item.strip() for item in self.mounts if item and item.strip()),
        )
        object.__setattr__(
            self,
            "command",
            tuple(item for item in self.command if item != ""),
        )

    def to_nerdctl_args(self, *, include_entrypoint: bool = True) -> list[str]:
        args: list[str] = []
        if self.gpus:
            args.extend(["--gpus", self.gpus])
        if self.ipc:
            args.extend(["--ipc", self.ipc])
        for item in self.env:
            args.extend(["-e", item])
        for item in self.mounts:
            args.extend(["--mount", item])
        if include_entrypoint and self.entrypoint:
            args.extend(["--entrypoint", self.entrypoint])
        return args

    def summary(self) -> dict[str, object]:
        return {
            "gpus": self.gpus or None,
            "ipc": self.ipc or None,
            "entrypoint": self.entrypoint or None,
            "env": list(self.env),
            "mounts": list(self.mounts),
            "command": list(self.command),
        }


def container_runtime_spec(
    *,
    gpus: str | None = None,
    ipc: str | None = None,
    entrypoint: str | None = None,
    env: list[str] | tuple[str, ...] | None = None,
    mounts: list[str] | tuple[str, ...] | None = None,
    command: list[str] | tuple[str, ...] | None = None,
) -> ContainerRuntimeSpec:
    return ContainerRuntimeSpec(
        gpus="" if gpus is None else gpus,
        ipc="" if ipc is None else ipc,
        entrypoint="" if entrypoint is None else entrypoint,
        env=tuple(env or ()),
        mounts=tuple(mounts or ()),
        command=tuple(command or ()),
    )


def inherit_container_runtime(
    base: ContainerRuntimeSpec,
    *,
    gpus: str | None = None,
    ipc: str | None = None,
    entrypoint: str | None = None,
    env: list[str] | tuple[str, ...] | None = None,
    mounts: list[str] | tuple[str, ...] | None = None,
    command: list[str] | tuple[str, ...] | None = None,
) -> ContainerRuntimeSpec:
    return ContainerRuntimeSpec(
        gpus=base.gpus if gpus is None else gpus,
        ipc=base.ipc if ipc is None else ipc,
        entrypoint=base.entrypoint if entrypoint is None else entrypoint,
        env=base.env if env is None else tuple(env),
        mounts=base.mounts if mounts is None else tuple(mounts),
        command=base.command if command is None else tuple(command),
    )
