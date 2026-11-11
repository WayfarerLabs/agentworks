"""The legacy half of ``vm shell --platform``.

``exec.py`` is migrated onto ``agentworks.execution`` and must import no
retired module, so the part of ``--platform`` that still builds a
platform-native ``Transport`` lives here. It stays legacy until the platform
surface replaces ``native_transport`` with a native shell.
"""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING

from agentworks.errors import StateError

if TYPE_CHECKING:
    import contextlib

    from agentworks.capabilities.base import RunContext
    from agentworks.capabilities.vm_platform import VMPlatform
    from agentworks.config import Config
    from agentworks.db import VMRow


def attach_via_native_transport(
    vm: VMRow,
    platform: VMPlatform,
    config: Config,
    *,
    ctx: RunContext,
    stack: contextlib.ExitStack,
    workspace_path: str | None,
    env: dict[str, str],
) -> int:
    """Open an interactive shell through the platform's own transport and return its exit code."""
    # Imported at call time, as the other legacy callers do, so tests patch the factory on its package.
    from agentworks.transports import Transport, native_transport

    target = native_transport(vm, platform, config, ctx=ctx, stack=stack)
    if not isinstance(target, Transport):
        raise StateError(
            f"VM platform '{platform.name}' declared native shell support but returned an execution-only transport.",
            entity_kind="vm",
            entity_name=vm.name,
            hint="Update Agentworks so the VM platform and core capability contract match.",
        )
    if workspace_path is not None:
        return target.interactive(f"cd {shlex.quote(workspace_path)} && exec $SHELL -l", env=env)
    return target.interactive("", env=env)
