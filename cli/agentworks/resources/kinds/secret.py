"""``SecretKind``: the framework's strategy for the ``"secret"`` kind.

Miss policy is ``auto-declare`` with no name restriction -- any name a
``SecretReference`` references will be auto-synthesized when not
operator-declared. The synthesized ``SecretDecl`` carries an empty
``description``; operators are warned (per FRD R9) that auto-declared
secrets should be promoted to explicit ``[secrets.<name>]`` blocks so they
can carry a description.
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

    from agentworks.db import Database, SessionRow
    from agentworks.resources.reference import ResourceReference
    from agentworks.resources.registry import Registry


SECRET_KIND_NAME = "secret"
"""Single source of truth for the ``"secret"`` kind identifier. Callers
that need to render or compare against the kind name import this rather
than re-typing the literal -- a hypothetical rename then flows through
every site by construction."""


@dataclass(frozen=True)
class _SecretKind:
    """Implementation of ``ResourceKind`` for ``"secret"``. Module-private;
    callers reach this through ``KIND_REGISTRY["secret"]``.
    """

    kind: str = SECRET_KIND_NAME
    miss_policy: Literal["auto-declare", "error"] = "auto-declare"
    auto_declare_names: frozenset[str] | None = None  # None = any name accepted

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
                "the secret kind has no reserved default name; "
                "synthesize requires at least one reference"
            )
        first = references[0]
        return SecretDecl(
            name=first.name,
            description="",
            origin=Origin.auto_declared(source=first.source),
        )

    def instances(
        self, db: Database, registry: Registry, resource: Any
    ) -> Iterable[InstanceRef]:
        """Sessions whose subgraph (per current config) reaches this
        secret. For each session row, we project its identity through
        the framework's reference walk: the session's session_template,
        the workspace's workspace_template, the VM's vm_template, the
        always-present admin_template, and (in agent mode) the agent's
        agent_template. Each root's reachable-secret set is collected;
        if this secret's name appears in the union for a given session,
        that session is emitted.

        The walk uses ``collect_secrets_for`` (the same helper
        ``vm create`` / ``agent create`` etc. use for eager-resolve), so
        the "what secrets would this session need?" answer is exactly
        the answer the orchestrator would compute at runtime -- modulo
        per-command scoping (e.g. ``vm reinit`` walks only the VM's
        subgraph). The result is *per current config*: edits to config
        change the projection immediately, even for sessions that were
        provisioned against a different config. See the Phase 3c
        "Forward-compat note" in the SDD plan.
        """
        target_name = resource.name
        for session in db.list_sessions():
            reachable = self._secrets_reachable_from_session(db, registry, session)
            if target_name in reachable:
                yield InstanceRef(
                    instance_kind="session", instance_name=session.name
                )

    @staticmethod
    def _secrets_reachable_from_session(
        db: Database, registry: Registry, session: SessionRow
    ) -> set[str]:
        roots: list[tuple[str, str]] = []
        roots.append(("session_template", session.template))
        # Every VM has an admin user that pulls from admin_template:default.
        # Sessions running on that VM transitively reach admin's env-block
        # references regardless of mode -- agent-mode sessions still log into
        # an admin-provisioned VM. Conservative for the projection.
        roots.append(("admin_template", "default"))
        workspace = db.get_workspace(session.workspace_name)
        if workspace is not None:
            roots.append(
                ("workspace_template", workspace.template or "default")
            )
            vm = db.get_vm(workspace.vm_name)
            if vm is not None:
                roots.append(("vm_template", vm.template or "default"))
        if session.mode == "agent" and session.agent_name is not None:
            agent = db.get_agent(session.agent_name)
            if agent is not None:
                roots.append(("agent_template", agent.template or "default"))

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


KIND_REGISTRY[SECRET_KIND_NAME] = _SecretKind()
