"""The legacy execution modules, named once for every guard that blocks them."""

from __future__ import annotations

LEGACY_MODULES = (
    "agentworks.transports",
    "agentworks.ssh",
    "agentworks.remote_exec",
    "agentworks.harness_setup.runner",
    "agentworks.native_files",
)


def is_legacy(name: str) -> bool:
    """Whether ``name`` is a legacy module or inside one."""
    return any(name == legacy or name.startswith(legacy + ".") for legacy in LEGACY_MODULES)
