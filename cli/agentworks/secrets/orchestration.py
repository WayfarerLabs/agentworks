"""Eager-prompting orchestration for secret-consuming commands.

Per FRD R4 and HLA "Eager prompting flow": every command that opens new
shells resolves all needed secrets up front (within the first few
seconds), before any state mutation. The resolver caches values across
the command, so subsequent ``compose_env`` / ``resolver.render`` calls
inside the command's body hit the cache and don't re-prompt.

Usage at a manager entry point:

    from agentworks.secrets.orchestration import SecretTarget, resolve_for_command

    targets = [
        SecretTarget(
            vm=vm_template.env,
            workspace=workspace_template.env,
            admin=config.admin.env,         # admin mode: only admin scope
            session=session_template.env,
        ),
    ]
    resolve_for_command(targets, config)  # raises on non-interactive miss
    # ... proceed with command execution; compose_env() hits the resolver cache.

The orchestrator is generic: it doesn't know about VMs, workspaces, or
agents. It just walks env dicts. Future legacy-prompt migrations
(Tailscale auth key, git credentials) hook in via ``extra_decls`` rather
than special-casing them in the orchestrator.

See ``docs/sdd/2026-06-05-env-and-secrets/`` for the full design.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from agentworks.env.merge import effective_env

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from agentworks.config import Config
    from agentworks.env.entry import EnvEntry
    from agentworks.secrets.base import SecretDecl


@dataclass(frozen=True)
class SecretTarget:
    """One shell-open site whose env chain may reference secrets.

    Fields mirror ``effective_env``: callers pass the per-scope env dicts
    they would have passed to ``effective_env`` for actual execution.
    Admin and agent are mutually exclusive (the merge layer enforces it).

    Targets do not carry DB rows. Callers resolve templates first and
    construct targets from the resulting env dicts, which keeps the
    orchestrator decoupled from DB / template-resolution code and makes
    unit tests trivially construct fake targets.
    """

    vm: dict[str, EnvEntry]
    workspace: dict[str, EnvEntry] | None = None
    admin: dict[str, EnvEntry] | None = None
    agent: dict[str, EnvEntry] | None = None
    session: dict[str, EnvEntry] | None = None
    label: str | None = field(default=None, compare=False)
    """Optional human-readable label for diagnostics. Not part of identity."""


def compute_needed_secrets(
    targets: Sequence[SecretTarget],
    config: Config,
    *,
    extra_decls: Iterable[SecretDecl] = (),
) -> list[SecretDecl]:
    """Union of ``SecretDecl``s referenced across the candidate target set.

    For each target, merges the per-scope env dicts via ``effective_env``
    (preserving the FRD R2 precedence ladder), asks the resolver which
    declared secrets that merged env references, and unions the results
    across all targets. ``extra_decls`` adds decls that aren't referenced
    by any target's env chain -- a hook for legacy-prompt migrations
    (e.g. Tailscale auth key, git credentials) that need eager resolution
    without being modeled as env-table entries today.

    The result preserves first-encounter order across targets, then
    extras, for deterministic prompting order.
    """
    resolver = config.secret_resolver
    seen: set[str] = set()
    out: list[SecretDecl] = []
    for target in targets:
        merged = effective_env(
            vm=target.vm,
            workspace=target.workspace,
            admin=target.admin,
            agent=target.agent,
            session=target.session,
        )
        for decl in resolver.required_for(merged):
            if decl.name not in seen:
                seen.add(decl.name)
                out.append(decl)
    for decl in extra_decls:
        if decl.name not in seen:
            seen.add(decl.name)
            out.append(decl)
    return out


def resolve_for_command(
    targets: Sequence[SecretTarget],
    config: Config,
    *,
    extra_decls: Iterable[SecretDecl] = (),
) -> None:
    """Eagerly resolve every secret referenced by the candidate targets.

    Computes the union of needed ``SecretDecl``s via
    ``compute_needed_secrets`` and resolves them in a single batched
    call through the configured backend chain. Values land in the
    resolver's cache; subsequent ``compose_env`` / ``resolver.render``
    calls inside the command hit the cache and never re-prompt.

    In non-interactive mode, missing secrets surface as
    ``SecretUnavailableError`` with a per-secret breakdown of which
    backends were tried. The error is raised before any state mutation
    so the operator can recover (set ``AW_SECRET_<NAME>``, add a
    ``backend_mappings`` entry, narrow the static filter, etc.) and
    re-run.

    Call this once at the head of any manager entry point that opens
    new shells. Pure inspection commands (``session attach``,
    ``session list``, ``console attach``, ``vm list``, etc.) MUST NOT
    call it -- they inherit the env captured at shell-create time and
    consume no secrets per FRD R4 / R5.
    """
    decls = compute_needed_secrets(targets, config, extra_decls=extra_decls)
    if not decls:
        return
    config.secret_resolver.resolve_all(decls)
