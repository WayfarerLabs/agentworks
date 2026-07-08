"""``_SessionTemplateKind`` and ``_NamedConsoleTemplateKind``: framework
strategies for the ``"session-template"`` and ``"named-console-template"``
kinds.

Both live in the ``sessions`` domain package next to the code that
implements sessions and named consoles;
``agentworks.resources.kinds.__init__`` imports this module so the kinds
self-register into ``KIND_REGISTRY`` at load.

``SessionTemplateKind`` has the same shape as the other template kinds.
``NamedConsoleTemplateKind`` is named-multi-instance in the framework
like everything else; the TOML shape is a singleton ``[named_console]``
block, published only when the operator declares it, and an undeclared
default is auto-declared by the always-materialize pre-step. Named
instances stay rejected at the manifest envelope until a console
selector exists (issue #165).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from agentworks.resources.kind import (
    ALWAYS_MATERIALIZE_SOURCE,
    KIND_REGISTRY,
    InstanceRef,
)
from agentworks.resources.origin import Origin
from agentworks.sessions.template import NamedConsoleConfig, SessionTemplate

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from agentworks.db import Database
    from agentworks.resources.reference import ResourceReference
    from agentworks.resources.registry import Registry


@dataclass(frozen=True)
class _SessionTemplateKind:
    """Implementation of ``ResourceKind`` for ``"session-template"``."""

    kind: str = "session-template"
    description: str = "Session configuration (command, restart, env)"
    miss_policy: Literal["auto-declare", "error"] = "auto-declare"
    auto_declare_names: frozenset[str] | None = frozenset({"default"})
    category: Literal["declarable", "capability"] = "declarable"
    builtin_override: Literal["allow", "reserved"] = "reserved"

    def synthesize(
        self,
        references: Sequence[ResourceReference],
    ) -> SessionTemplate:
        """Build the code-defined default ``SessionTemplate``. See
        ``agentworks.vms.kinds``'s ``synthesize`` for the rationale on why
        the non-empty-``references`` path is preserved.
        """
        source = references[0].source if references else ALWAYS_MATERIALIZE_SOURCE
        return SessionTemplate(
            name="default", origin=Origin.auto_declared(source=source)
        )

    def instances(
        self, db: Database, registry: Registry, resource: Any
    ) -> Iterable[InstanceRef]:
        """Every session whose ``template`` column matches this
        SessionTemplate's name. ``SessionRow.template`` is non-optional,
        so the NULL-as-default fallback used by other template kinds
        doesn't apply here -- sessions are always created with an
        explicit template value (``default`` when none is specified).
        """
        name = resource.name
        for sess in db.list_sessions():
            if sess.template == name:
                yield InstanceRef(instance_kind="session", instance_name=sess.name)


@dataclass(frozen=True)
class _NamedConsoleTemplateKind:
    """Implementation of ``ResourceKind`` for ``"named-console-template"``."""

    kind: str = "named-console-template"
    description: str = "Named console configuration (layout, ...)"
    miss_policy: Literal["auto-declare", "error"] = "auto-declare"
    auto_declare_names: frozenset[str] | None = frozenset({"default"})
    category: Literal["declarable", "capability"] = "declarable"
    builtin_override: Literal["allow", "reserved"] = "reserved"

    def synthesize(
        self,
        references: Sequence[ResourceReference],
    ) -> NamedConsoleConfig:
        """Build an empty-defaults ``NamedConsoleConfig`` for an
        auto-declared ``named-console-template:default``. Same shape as
        the admin template kind: the routine path whenever the operator
        declares no ``[named_console]`` section and no manifest.

        Tolerates ``references=()`` per the Phase 2a contract; uses
        the synthetic ``("framework", "always-materialize")`` source
        when called that way.
        """
        source = references[0].source if references else ALWAYS_MATERIALIZE_SOURCE
        return NamedConsoleConfig(origin=Origin.auto_declared(source=source))

    def instances(
        self, db: Database, registry: Registry, resource: Any
    ) -> Iterable[InstanceRef]:
        """Every console implicitly uses the singleton
        ``named-console-template:default`` -- there's no per-console
        template column on the operator surface yet, and Phase 2a.3
        deferred plurifying ``NamedConsoleConfig`` (no ``name`` field),
        so the kind is effectively a singleton today. When operator
        demand for named console templates lands, plurify
        ``NamedConsoleConfig`` (mirror the Phase 2a.3 admin change) and
        switch this filter to ``resource.name`` + a ``console.template``
        column.

        Asymmetry with ``admin-template``: that kind guards with
        ``if resource.name != "default": return`` because ``AdminConfig``
        has a ``name`` field (Phase 2a.3 plurified it). ``NamedConsoleConfig``
        doesn't have a ``name`` field yet, and the registry refuses any
        non-``default`` named-console-template name via miss-policy
        dispatch, so the guard isn't reachable today. When the plurified
        surface lands and ``NamedConsoleConfig`` gains a ``name`` field,
        mirror the ``admin-template`` shape exactly.
        """
        for console in db.list_consoles():
            yield InstanceRef(instance_kind="console", instance_name=console.name)


KIND_REGISTRY["session-template"] = _SessionTemplateKind()
KIND_REGISTRY["named-console-template"] = _NamedConsoleTemplateKind()
