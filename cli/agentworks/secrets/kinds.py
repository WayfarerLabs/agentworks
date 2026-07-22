"""Framework strategies for the ``"secret"`` and ``"secret-backend"``
kinds, plus the ``SECRET_KIND_NAME`` identifier and the
``SecretBackendEntry`` capability row.

Both live in the ``secrets`` domain package next to the code that
implements secrets and backends; ``agentworks.resources.kinds.__init__``
imports this module so the kinds self-register into ``KIND_REGISTRY`` at
load. ``SecretDecl`` (the declarable row) already lives in
``agentworks.secrets.base`` -- imported from there as today.

``SecretKind`` uses the ``auto-declare`` miss policy with no name
restriction -- any name a ``SecretReference`` references is
auto-synthesized when not operator-declared. The synthesized
``SecretDecl`` carries an empty ``description``; operators are warned
that auto-declared secrets should be promoted to explicit
``[secrets.<name>]`` blocks so they can carry a description.

``SecretBackendKind`` is a read-only capability kind. Backends are code
capabilities (``agentworks.secrets.backends``); the registry rows exist
so the ``[secret_config].backends`` chain and per-secret
``backend_mappings`` validate through the framework's uniform miss
policy and the backends are visible in ``agw resource list``. Not
manifest-declarable (ADR 0016).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from agentworks.resources.kind import (
    KIND_REGISTRY,
    InstanceRef,
    NoUnreferencedDefaultError,
)
from agentworks.resources.origin import Origin
from agentworks.resources.walk import collect_secrets_for
from agentworks.secrets.base import SecretDecl

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from agentworks.db import Database, SessionRow, VMRow
    from agentworks.resources.reference import ReferenceEntry, ResourceReference
    from agentworks.resources.registry import Registry


SECRET_KIND_NAME = "secret"
"""Single source of truth for the ``"secret"`` kind identifier. Callers
that need to render or compare against the kind name import this rather
than re-typing the literal -- a hypothetical rename then flows through
every site by construction."""


@dataclass(frozen=True)
class SecretBackendEntry:
    """The capability resource for one registered secret backend.

    The actual capability (the ``SecretBackend`` API) lives in
    ``agentworks.secrets.backends.SECRET_BACKEND_REGISTRY``; this row is
    what the chain and mapping names resolve against in the framework.
    ``description`` comes from the capability, for inspection surfaces.
    """

    name: str
    description: str = ""
    origin: Origin | None = None
    references: tuple[ReferenceEntry, ...] = ()


@dataclass(frozen=True)
class _SecretKind:
    """Implementation of ``ResourceKind`` for ``"secret"``. Module-private;
    callers reach this through ``KIND_REGISTRY["secret"]``.
    """

    kind: str = SECRET_KIND_NAME
    description: str = "Declared secrets and their backend mappings"
    miss_policy: Literal["auto-declare", "error"] = "auto-declare"
    auto_declare_names: frozenset[str] | None = None  # None = any name accepted
    category: Literal["declarable", "capability"] = "declarable"
    builtin_override: Literal["allow", "reserved"] = "reserved"

    def synthesize(self, references: Sequence[ResourceReference]) -> SecretDecl:
        """Build a ``SecretDecl`` for an auto-declared secret. ``references``
        is non-empty in normal operation (the Registry calls ``synthesize``
        only when an incoming reference triggered the miss policy) and
        ordered by config-load walk order.

        Only ``origin`` (auto-declared, source = first matching
        reference's source) is attached here. ``usage`` is centralized
        in ``Registry.finalize``'s post-stabilization pass so the kind
        doesn't need to know the final reference map -- a synthesized
        Resource that goes on to publish references of its own may
        gather later incoming edges that this initial call can't see.

        Raises ``NoUnreferencedDefaultError`` if called with
        ``references=()`` -- the secret kind has no concept of an
        unreferenced default (``auto_declare_names = None``), so the
        framework never calls this path; the explicit error is defensive
        in case the kind's auto-declare configuration ever changes.
        """
        if not references:
            raise NoUnreferencedDefaultError(
                "the secret kind has no reserved default name; synthesize requires at least one reference"
            )
        first = references[0]
        return SecretDecl(
            name=first.name,
            description="",
            origin=Origin.auto_declared(source=first.source),
        )

    def instances(self, db: Database, registry: Registry, resource: Any) -> Iterable[InstanceRef]:
        """Sessions whose subgraph (per current config) reaches this
        secret. For each session row, we project its identity through
        the framework's reference walk: the session's session_template,
        the workspace's workspace_template, the VM's vm_template, and
        -- mutually exclusive by session mode -- either admin_template
        (admin-mode) or the agent's agent_template (agent-mode). Each
        root's reachable-secret set is collected; if this secret's name
        appears in the union for a given session, that session is
        emitted. See ``_secrets_reachable_from_session`` for the full
        env-and-secrets layering rationale.

        The walk uses ``collect_secrets_for`` (the same helper
        ``vm create`` / ``agent create`` etc. use for eager-resolve), so
        the "what secrets would this session need?" answer is exactly
        the answer the orchestrator would compute at runtime -- modulo
        per-command scoping (e.g. ``vm reinit`` walks only the VM's
        subgraph). The result is *per current config*: edits to config
        change the projection immediately, even for sessions that were
        provisioned against a different config.
        """
        target_name = resource.name
        for session in db.list_sessions():
            reachable = self._secrets_reachable_from_session(db, registry, session)
            if target_name in reachable:
                yield InstanceRef(instance_kind="session", instance_name=session.name)

    @staticmethod
    def _secrets_reachable_from_session(db: Database, registry: Registry, session: SessionRow) -> set[str]:
        """Build the set of secret names a session's shell would see in
        its env per current config. Roots follow the env-and-secrets
        layering: a session's shell sees ``vm + workspace + (admin |
        agent) + session`` env -- mode picks exactly one of admin-template
        or agent_template, not both.

        Note: this answers "what would this session's shell env contain?"
        not "what does this session's VM need to be provisioned with?".
        A secret referenced only from ``[admin.env]`` is NOT counted as
        "used by" an agent-mode session even though the VM's admin user
        needs it for ``agw vm shell``. The projection is operator-facing
        ("does my agent see this secret?"), and the admin user's own
        dependencies surface via admin-template's own ``Used by:`` entry
        (every VM).

        ``vm-template`` is always included because the session's VM
        bootstrap (apt packages, tailscale auth key, etc.) is a hard
        dependency regardless of session mode.
        """
        roots: list[tuple[str, str]] = []
        roots.append(("session-template", session.template))
        # Hoisted so the admin branch below can read the VM's
        # admin-template column (the workspace block is the only place the
        # VM row resolves).
        vm: VMRow | None = None
        workspace = db.get_workspace(session.workspace_name)
        if workspace is not None:
            roots.append(("workspace-template", workspace.template or "default"))
            vm = db.get_vm(workspace.vm_name)
            if vm is not None:
                roots.append(("vm-template", vm.template or "default"))
        # Mode picks exactly one of admin-template / agent-template. Admin
        # mode reads the VM's per-VM admin-template column (NULL column =
        # reserved ``default``); a session whose VM row is missing falls
        # back to ``default``.
        if session.mode == "admin":
            roots.append(("admin-template", (vm.admin_template if vm else None) or "default"))
        elif session.mode == "agent" and session.agent_name is not None:
            agent = db.get_agent(session.agent_name)
            if agent is not None:
                roots.append(("agent-template", agent.template or "default"))

        names: set[str] = set()
        for root in roots:
            try:
                for decl in collect_secrets_for(registry, root):
                    names.add(decl.name)
            except KeyError:
                # Defensive: a root that doesn't resolve in the registry
                # means the underlying template wasn't published (e.g. a
                # session whose template was renamed in config). Skip the
                # missing root rather than blowing up the entire inspection.
                continue
        return names


@dataclass(frozen=True)
class _SecretBackendKind:
    """Implementation of ``ResourceKind`` for ``"secret-backend"``."""

    kind: str = "secret-backend"
    description: str = "Capability for resolving secret values"
    miss_policy: Literal["auto-declare", "error"] = "error"
    auto_declare_names: frozenset[str] | None = None
    category: Literal["declarable", "capability"] = "capability"
    builtin_override: Literal["allow", "reserved"] = "reserved"

    def synthesize(self, references: Sequence[ResourceReference]) -> SecretBackendEntry:
        raise NoUnreferencedDefaultError(
            "the secret-backend kind has miss_policy='error'; synthesize should never be dispatched"
        )


KIND_REGISTRY[SECRET_KIND_NAME] = _SecretKind()
KIND_REGISTRY["secret-backend"] = _SecretBackendKind()
