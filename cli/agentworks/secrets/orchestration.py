"""Env-chain secret collection for the boundary resolve.

A command resolves all the secrets its plan needs in one boundary pass
per composition root, before any state mutation. This module supplies
the env-chain half of that union: ``SecretTarget`` describes a
workload's merged per-scope env dicts, ``compute_needed_secrets`` walks
them for secret references, and the orchestrated composition roots
register the result on the operation's boundary resolver
(``Resolver.register_targets``) so runtime env secrets join the same
pass as the graph's config and token secrets. ``resolve_for_command``
is the standalone form (collect and resolve in one call) for callers
outside an orchestrated root; the returned values dict travels down to
every ``compose_env`` site, so nothing re-prompts by construction (no
cache exists to hit or miss).

Usage at a manager entry point:

    from agentworks.secrets.orchestration import SecretTarget, resolve_for_command

    targets = [
        SecretTarget(
            vm=vm_template.env,
            workspace=workspace_template.env,
            admin=admin_template(registry).env,  # admin mode: only admin scope
            session=session_template.env,
        ),
    ]
    values = resolve_for_command(targets, config, registry)  # raises on non-interactive miss
    # ... thread `values` down to every compose_env(values=...) site.

The orchestrator is generic: it doesn't know about VMs, workspaces, or
agents. It just walks env dicts. Future legacy-prompt migrations
(Tailscale auth key, git credentials) hook in via ``extra_decls`` rather
than special-casing them in the orchestrator.

**Substitution invariance:** callers may hand in either pre- or
post-substitution env dicts (e.g. before or after
``_substitute_template_vars_in_env``). The computed union of
``SecretDecl``s is invariant under substitution because substitution
only rewrites ``EnvEntry.value`` (plaintext), never ``EnvEntry.secret``
(the reference name). This is load-bearing for callers that build
targets from un-substituted template env dicts.

**Non-interactive errors:** ``resolve_for_command`` raises
``SecretUnavailableError`` if the active backends can't satisfy a
secret (e.g. ``--non-interactive`` + no ``AW_SECRET_<NAME>`` set). The
error carries a per-secret hint listing the backends tried. Manager-
layer callers may catch and re-raise with command-level context
(``entity_kind`` / ``entity_name``) if useful, but the default error
shape is already operator-actionable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from agentworks.env.merge import effective_env
from agentworks.errors import StateError

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from agentworks.config import Config
    from agentworks.env.entry import EnvEntry
    from agentworks.resources.registry import Registry
    from agentworks.secrets.base import SecretDecl


@dataclass(frozen=True)
class SecretTarget:
    """One shell-open site whose env chain may reference secrets.

    Fields mirror ``effective_env``: callers pass the per-scope env dicts
    they would have passed to ``effective_env`` for actual execution.
    Admin and agent are mutually exclusive (the merge layer raises
    ``ValueError`` if both are set).

    Targets do not carry DB rows. Callers resolve templates first and
    construct targets from the resulting env dicts, which keeps the
    orchestrator decoupled from DB / template-resolution code and makes
    unit tests trivially construct fake targets.

    Equality: ``label`` is excluded. Two targets with the same env
    dicts but different labels compare equal. Hashing is not supported
    -- the dataclass is frozen but the env fields are mutable dicts,
    so ``hash(target)`` raises ``TypeError``. Callers that need to
    dedupe targets must do it by env content, not via ``set``.
    """

    vm: dict[str, EnvEntry]
    workspace: dict[str, EnvEntry] | None = None
    admin: dict[str, EnvEntry] | None = None
    agent: dict[str, EnvEntry] | None = None
    session: dict[str, EnvEntry] | None = None
    label: str | None = field(default=None, compare=False)
    """Optional human-readable label for diagnostics. Not part of
    equality, hashing, or identity."""


def compute_needed_secrets(
    targets: Sequence[SecretTarget],
    registry: Registry,
    *,
    extra_decls: Iterable[SecretDecl] = (),
) -> list[SecretDecl]:
    """Union of ``SecretDecl``s referenced across the candidate target set.

    For each target, merges the per-scope env dicts via ``effective_env``
    (preserving the scope precedence ladder
    ``session > (agent | admin) > workspace > vm``), collects the declared
    secrets those merged env dicts reference, and unions the results
    across all targets. ``extra_decls`` adds decls that aren't referenced
    by any target's env chain: a hook for legacy-prompt migrations
    (e.g. Tailscale auth key, git credentials) that need eager resolution
    without being modeled as env-table entries today.

    The result preserves first-encounter order across targets, then
    extras, for deterministic prompting order. Referenced names are
    looked up against the registry's ``secret`` rows -- which, after
    finalize, cover operator-declared AND auto-declared secrets, so
    every name a published template's env references has a decl here.
    A miss therefore means a reference that failed to auto-declare (a
    publisher/finalize bug, or a hand-built registry that skipped it)
    and raises loudly: silently dropping the name would surface later
    as a mysterious "secret didn't resolve" far from the cause.
    """
    from agentworks.resources.access import secret_decls

    decls = secret_decls(registry)
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
        for key, entry in merged.items():
            name = entry.secret
            if name is None or name in seen:
                continue
            decl = decls.get(name)
            if decl is None:
                # StateError, not ConfigError: the message is right that
                # this is never an operator's config mistake (referenced
                # secrets auto-declare at finalize), so it must not wear
                # the operator-config error kind.
                raise StateError(
                    f"env var {key!r} ({target.label}) references secret "
                    f"{name!r}, which has no declaration in the registry. "
                    f"Referenced secrets auto-declare at finalize, so this "
                    f"is a registry-construction bug, not an operator error.",
                )
            seen.add(name)
            out.append(decl)
    for decl in extra_decls:
        if decl.name not in seen:
            seen.add(decl.name)
            out.append(decl)
    return out


def resolve_for_command(
    targets: Sequence[SecretTarget],
    config: Config,
    registry: Registry,
    *,
    extra_decls: Iterable[SecretDecl] = (),
) -> dict[str, str]:
    """Resolve every secret referenced by the candidate targets: THE
    command's one resolve call.

    Computes the union of needed ``SecretDecl``s via
    ``compute_needed_secrets`` and runs the resolve loop over the active
    backends once. The returned ``{secret_name: value}`` mapping is the
    ONLY channel -- there is no cache. The command threads the values
    down to its ``compose_env`` sites (the same scope-dict discipline
    that keeps the eager-resolve set and the render set from drifting);
    "prompt-once" holds by construction because this is called once per
    command. Empty target union returns ``{}`` without consulting any
    backend.

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
    consume no secrets.
    """
    decls = compute_needed_secrets(targets, registry, extra_decls=extra_decls)
    if not decls:
        return {}
    from agentworks.secrets.resolve import active_backends, resolve_secrets

    return resolve_secrets(decls, active_backends(config, registry))
