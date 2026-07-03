"""``SessionTemplateKind``: framework strategy for the
``"session-template"`` kind. Same shape as the other template kinds.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from agentworks.config import SessionTemplate
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
class _SessionTemplateKind:
    """Implementation of ``ResourceKind`` for ``"session-template"``."""

    kind: str = "session-template"
    miss_policy: Literal["auto-declare", "error"] = "auto-declare"
    auto_declare_names: frozenset[str] | None = frozenset({"default"})
    manifest_declarable: bool = True
    builtin_override: Literal["allow", "reserved"] = "reserved"

    def synthesize(
        self,
        references: Sequence[ResourceReference],
    ) -> SessionTemplate:
        """Build the code-defined default ``SessionTemplate``. See
        ``vm_template.py``'s ``synthesize`` for the rationale on why the
        non-empty-``references`` path is preserved.
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


KIND_REGISTRY["session-template"] = _SessionTemplateKind()
