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
from typing import Literal

SessionKind = Literal["admin", "agent"]


@dataclass(frozen=True)
class ResourceContext:
    """The resource chain that scopes a shell.

    ``vm_name`` / ``platform`` / ``user`` are always present for an on-VM
    shell. ``vm_host`` is the name from the ``vm_hosts`` registry (e.g.
    ``"lima-local"``); only Lima VMs are tied to a registry entry, so the
    field is ``None`` for VMs without one. The remaining fields are
    present when the corresponding scope applies (workspace context,
    agent context, session context).

    ``session_kind`` is ``"admin"`` when the session runs as the admin user
    and ``"agent"`` when it runs as an agent user. It is set whenever
    ``session_name`` is set; the loader / caller enforces this invariant.
    """

    vm_name: str
    platform: str
    user: str
    vm_host: str | None = None
    workspace_name: str | None = None
    workspace_dir: str | None = None
    agent_name: str | None = None
    session_name: str | None = None
    session_kind: SessionKind | None = None


def agentworks_identity_env(ctx: ResourceContext) -> dict[str, str]:
    """Return ALL AGENTWORKS_* vars that apply to ``ctx``.

    Includes the full identity set: VM-stable vars (VM / VM_HOST / PLATFORM),
    per-user vars (USER), and per-context vars (WORKSPACE[_DIR] / AGENT /
    SESSION[_KIND]). Use the focused helpers below to select the subset
    appropriate for a particular write site (SSH SetEnv vs profile
    fragments).
    """
    out = {
        **vm_stable_identity_env(ctx),
        **per_user_identity_env(ctx),
        **per_context_identity_env(ctx),
    }
    return out


def vm_stable_identity_env(ctx: ResourceContext) -> dict[str, str]:
    """VM-stable subset, written to ``/etc/profile.d/agentworks-identity.sh``.

    The same on every Linux user on the VM and every shell that VM hosts.
    Phase 4 of the env-and-secrets effort writes these to a system-wide
    profile fragment so that any shell on the VM (including raw ssh logins)
    sees them.

    ``AGENTWORKS_VM_HOST`` is only emitted when the VM has a registered
    host (Lima VMs may; Azure / WSL2 / Proxmox VMs do not, per the
    ``vm_hosts`` registry).
    """
    out = {
        "AGENTWORKS_VM": ctx.vm_name,
        "AGENTWORKS_PLATFORM": ctx.platform,
    }
    if ctx.vm_host is not None:
        out["AGENTWORKS_VM_HOST"] = ctx.vm_host
    return out


def per_user_identity_env(ctx: ResourceContext) -> dict[str, str]:
    """Per-user subset, written to ``~/.agentworks-profile.sh``.

    Each Linux user on the VM gets their own copy of this fragment with
    AGENTWORKS_USER set to their username.
    """
    return {"AGENTWORKS_USER": ctx.user}


def per_context_identity_env(ctx: ResourceContext) -> dict[str, str]:
    """Per-context subset, injected inline at shell-open time.

    Values vary per shell-open invocation (workspace / agent / session
    context), so these cannot live in a VM-side profile fragment.
    """
    out: dict[str, str] = {}
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
