"""Framework strategy for the declarable ``secret`` resource kind.

``SecretDecl`` lives in :mod:`agentworks.secrets.base`; the capability-owned
``secret-backend`` strategy lives beside its implementation contract under
:mod:`agentworks.capabilities.secret_backend.kinds`.

``SecretKind`` uses the ``auto-declare`` miss policy with no name
restriction -- any name a ``SecretReference`` references is
auto-synthesized when not operator-declared. The synthesized
``SecretDecl`` carries an empty ``description``; operators are warned
that auto-declared secrets should be promoted to explicit
``[secrets.<name>]`` blocks so they can carry a description.

"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from agentworks.origin import Origin
from agentworks.resources.kind import (
    KIND_REGISTRY,
    InstanceRef,
    NoUnreferencedDefaultError,
)
from agentworks.resources.walk import collect_secrets_for
from agentworks.secrets.base import SecretDecl
from agentworks.topics import TopicProse

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from agentworks.db import Database, SessionRow, VMRow
    from agentworks.declared_resource import DeclaredResource
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
    model: type[DeclaredResource] = SecretDecl
    description: str = "Declared secrets and their backend mappings"
    prose: TopicProse = TopicProse(
        title="Secrets",
        overview="""
        A secret is a NAME, not a value. Declaring one says a value by that name exists,
        what it is for, and (optionally) what each backend calls it; the value itself is
        produced by a secret-backend at command time and never stored by agentworks.

        Anything that needs a secret refers to it by name: an `env` table writes
        `{secret: npm-token}`, and a capability config field that names a secret (a git
        credential's token, a platform's client secret) takes the name too. A referenced
        secret that nothing declared is auto-declared, so declaring one is how you give
        it a description and a hint, which are the text an operator reads when they are
        asked to type the value in.

        `backend_mappings` overrides what one backend calls this secret, or opts out of
        that backend entirely with `false`. Run
        `agw resource describe-kind secret-backend` to see which backends this host has.
        """,
    )
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
            # The DECLARER: an inheriting template publishes its merged
            # declaration's secrets, so the row that WROTE the name is the
            # provenance an operator can act on.
            origin=Origin.auto_declared(source=first.declarer),
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

        The walk uses ``collect_secrets_for``, the framework's
        runtime-need closure over the graph, so the "what secrets would
        this session need?" answer is derived from the same edges every
        other structural surface reads rather than from a second walk of
        its own. The orchestrator's runtime union is computed differently
        (off a plan's already-resolved nodes), which is why this is a
        projection rather than a prediction. The result is *per current
        config*: edits to config change it immediately, even for sessions
        that were provisioned against a different config.
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


KIND_REGISTRY[SECRET_KIND_NAME] = _SecretKind()
