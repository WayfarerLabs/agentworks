"""AGENTWORKS_* identity env vars.

A pure-data producer: takes a ResourceContext describing the shell's scope
and returns the subset of AGENTWORKS_* vars that apply. No I/O, no config
reads. The on-VM profile fragment writer (vms/initializer.py, Phase 4)
calls this with the VM-level subset; runtime shell-opens (sessions,
consoles, exec, agent-shell) call it with the full chain.

See FRD R1 for the full var table and the "Set when" predicates.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ResourceContext:
    """The resource chain that scopes a shell.

    ``vm_name`` / ``vm_host`` / ``platform`` / ``user`` are always present
    for an on-VM shell. The remaining fields are present when the
    corresponding scope applies (workspace context, agent context, session
    context).

    ``session_kind`` is "admin" when the session runs as the admin user and
    "agent" when it runs as an agent user. It is set whenever
    ``session_name`` is set; the loader / caller enforces this invariant.
    """

    vm_name: str
    vm_host: str
    platform: str
    user: str
    workspace_name: str | None = None
    workspace_dir: str | None = None
    agent_name: str | None = None
    session_name: str | None = None
    session_kind: str | None = None


def agentworks_identity_env(ctx: ResourceContext) -> dict[str, str]:
    """Return the AGENTWORKS_* vars that apply to ``ctx``.

    Always includes AGENTWORKS_VM, AGENTWORKS_VM_HOST, AGENTWORKS_PLATFORM,
    AGENTWORKS_USER. Adds AGENTWORKS_WORKSPACE[_DIR] / AGENTWORKS_AGENT /
    AGENTWORKS_SESSION[_KIND] when the corresponding scope is set.
    """
    out = {
        "AGENTWORKS_VM": ctx.vm_name,
        "AGENTWORKS_VM_HOST": ctx.vm_host,
        "AGENTWORKS_PLATFORM": ctx.platform,
        "AGENTWORKS_USER": ctx.user,
    }
    if ctx.workspace_name is not None:
        out["AGENTWORKS_WORKSPACE"] = ctx.workspace_name
    if ctx.workspace_dir is not None:
        out["AGENTWORKS_WORKSPACE_DIR"] = ctx.workspace_dir
    if ctx.agent_name is not None:
        out["AGENTWORKS_AGENT"] = ctx.agent_name
    if ctx.session_name is not None:
        out["AGENTWORKS_SESSION"] = ctx.session_name
        if ctx.session_kind is not None:
            out["AGENTWORKS_SESSION_KIND"] = ctx.session_kind
    return out
