"""``NamedConsoleTemplateKind``: the framework's strategy for the
``"named-console-template"`` kind.

Operator-surface-singleton today: ``Config.publish_to`` always publishes
``named-console-template:default`` (even when no ``[named_console]``
section exists), so the auto-declare path is a safety net for typo'd
references rather than a routine occurrence. Phase 2a.3 plurified
``admin-template`` at the framework level (matching the other template
kinds) but deliberately scoped that change to admin only; this kind
follows when there's an operator need to declare named console
templates. The kind shape (``auto_declare_names = {"default"}``,
synthesize tolerating ``references=()``) is already aligned with the
named-multi-instance template kinds, so plurifying the operator surface
later is a parser/loader change, not a framework change.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from agentworks.config import NamedConsoleConfig
from agentworks.resources.kind import (
    ALWAYS_MATERIALIZE_SOURCE,
    KIND_REGISTRY,
    InstanceRef,
)
from agentworks.resources.origin import Origin

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from agentworks.db import Database
    from agentworks.resources.reference import ResourceReference
    from agentworks.resources.registry import Registry


@dataclass(frozen=True)
class _NamedConsoleTemplateKind:
    """Implementation of ``ResourceKind`` for ``"named-console-template"``."""

    kind: str = "named-console-template"
    miss_policy: Literal["auto-declare", "error"] = "auto-declare"
    auto_declare_names: frozenset[str] | None = frozenset({"default"})
    manifest_declarable: bool = True
    builtin_override: Literal["allow", "reserved"] = "reserved"

    def synthesize(
        self,
        references: Sequence[ResourceReference],
    ) -> NamedConsoleConfig:
        """Build an empty-defaults ``NamedConsoleConfig`` for an
        auto-declared ``named-console-template:default``. Same shape as
        the admin template kind: ``Config.publish_to`` always publishes
        a real one so the always-materialize pre-step short-circuits.

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


KIND_REGISTRY["named-console-template"] = _NamedConsoleTemplateKind()
