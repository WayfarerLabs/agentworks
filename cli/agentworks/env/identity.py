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

    ``vm_name`` / ``platform`` / ``site`` / ``user`` are always present
    for an on-VM shell. ``platform`` carries the capability name
    (``lima`` / ``wsl2`` / ``azure`` / ``proxmox``), resolved at the
    caller's composition root via the site declaration; ``site`` is the
    vm-site resource name from ``vm.site``. The remaining fields are
    present when the corresponding scope applies (workspace context,
    agent context, session context).

    ``session_kind`` is ``"admin"`` when the session runs as the admin user
    and ``"agent"`` when it runs as an agent user. It is set whenever
    ``session_name`` is set; the loader / caller enforces this invariant.
    """

    vm_name: str
    platform: str
    site: str
    user: str
    workspace_name: str | None = None
    workspace_dir: str | None = None
    agent_name: str | None = None
    session_name: str | None = None
    session_kind: SessionKind | None = None


def agentworks_identity_env(ctx: ResourceContext) -> dict[str, str]:
    """Return ALL AGENTWORKS_* vars that apply to ``ctx``.

    Identity vars split into three kinds:

    - **VM-stable** (``vm_stable_identity_env``): same for every shell on
      the VM; lives in ``/etc/profile.d/agentworks-identity.sh``.
    - **Per-user static** (``per_user_identity_env``): same for every shell
      as a given Linux user; lives in ``~/.agentworks-profile.sh``. Today
      this is ``AGENTWORKS_AGENT`` for agent users only -- admins get the
      empty dict (their identity is the standard ``$USER`` / ``$LOGNAME``).
    - **Per-context dynamic** (``per_context_identity_env``): varies per
      shell-open; injected via SSH SetEnv. Today this is the workspace /
      session context vars.

    The on-VM Linux user is already exposed by the standard ``$USER`` /
    ``$LOGNAME`` env vars; we don't shadow those with an AGENTWORKS_-
    prefixed copy.
    """
    return {
        **vm_stable_identity_env(ctx),
        **per_user_identity_env(ctx),
        **per_context_identity_env(ctx),
    }


def vm_stable_identity_env(ctx: ResourceContext) -> dict[str, str]:
    """VM-stable subset, written to ``/etc/profile.d/agentworks-identity.sh``.

    The same on every Linux user on the VM and every shell that VM hosts.
    Phase 4 of the env-and-secrets effort writes these to a system-wide
    profile fragment so that any shell on the VM (including raw ssh logins)
    sees them.

    Per the vm-sites SDD (R12): ``AGENTWORKS_PLATFORM`` keeps its name
    and values (the capability name it has always carried);
    ``AGENTWORKS_SITE`` is new; ``AGENTWORKS_VM_HOST`` retired with the
    ``vm_hosts`` registry (the site name conveys the same information).
    """
    return {
        "AGENTWORKS_VM": ctx.vm_name,
        "AGENTWORKS_PLATFORM": ctx.platform,
        "AGENTWORKS_SITE": ctx.site,
    }


def per_user_identity_env(ctx: ResourceContext) -> dict[str, str]:
    """Per-user static subset, written to ``~/.agentworks-profile.sh``.

    The same value every time a given Linux user logs in. Today this is
    ``AGENTWORKS_AGENT`` for agent users only -- admin users get an empty
    dict (their identity is the standard ``$USER`` / ``$LOGNAME``).

    The agent's setup writes this fragment before the agent's install
    commands run, so install commands see ``AGENTWORKS_AGENT`` via the
    standard login-shell sourcing chain (no SetEnv injection needed at
    provisioning time, no SetEnv injection needed at runtime either:
    every login shell as that user sources the fragment).
    """
    out: dict[str, str] = {}
    if ctx.agent_name is not None:
        out["AGENTWORKS_AGENT"] = ctx.agent_name
    return out


def per_context_identity_env(ctx: ResourceContext) -> dict[str, str]:
    """Per-context dynamic subset, injected via SSH SetEnv at shell-open.

    Values vary per shell-open invocation (workspace / session context),
    so these can't live in an on-disk profile fragment. Agent identity is
    static at the user level, not per-context; see ``per_user_identity_env``.
    """
    out: dict[str, str] = {}
    if ctx.workspace_name is not None:
        out["AGENTWORKS_WORKSPACE"] = ctx.workspace_name
    if ctx.workspace_dir is not None:
        out["AGENTWORKS_WORKSPACE_DIR"] = ctx.workspace_dir
    if ctx.session_name is not None:
        out["AGENTWORKS_SESSION"] = ctx.session_name
        if ctx.session_kind is not None:
            out["AGENTWORKS_SESSION_KIND"] = ctx.session_kind
    return out
