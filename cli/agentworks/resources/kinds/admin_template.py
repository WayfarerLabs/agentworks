"""``AdminTemplateKind``: the framework's strategy for the
``"admin-template"`` kind.

Phase 2a.3 plurified this kind from singleton-conceptual to
named-multi-instance, matching the shape of ``vm-template`` /
``agent-template`` / ``workspace-template`` / ``session-template``. The
operator-facing surface is unchanged in Phase 2a: the loader still only
accepts the singleton ``[admin]`` block and publishes one
``admin-template:default`` row. A future SDD adds
``[admin_templates.<name>]`` parsing, the ``--admin-template`` CLI flag,
and the VM DB column without re-touching the framework.

Miss policy is ``auto-declare`` with reserved name ``"default"`` -- the
always-materialize pre-step seeds ``admin-template:default`` when it
isn't already published (Config publishes the empty-defaults instance
even when no ``[admin.*]`` sections exist, so the pre-step usually
short-circuits). Typo'd names like ``admin-template:custom`` referenced
from somewhere error via the framework's miss-policy dispatch.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from agentworks.config import AdminConfig
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
class _AdminTemplateKind:
    """Implementation of ``ResourceKind`` for ``"admin-template"``."""

    kind: str = "admin-template"
    miss_policy: Literal["auto-declare", "error"] = "auto-declare"
    auto_declare_names: frozenset[str] | None = frozenset({"default"})

    def synthesize(self, references: Sequence[ResourceReference]) -> AdminConfig:
        """Build an empty-defaults ``AdminConfig`` for an auto-declared
        ``admin-template:default``.

        Rarely actually called: ``Config.publish_to`` always publishes a
        real ``admin-template:default`` from ``Config.admin`` (even when
        the operator omits every ``[admin.*]`` section -- the loader
        synthesizes an empty-defaults instance), so the always-materialize
        pre-step's "is the name already in the registry?" short-circuits
        before reaching this method. See ``vm_template.py``'s
        ``synthesize`` for the rationale on why the non-empty-
        ``references`` path is preserved.
        """
        source = references[0].source if references else ALWAYS_MATERIALIZE_SOURCE
        return AdminConfig(name="default", origin=Origin.auto_declared(source=source))

    def instances(
        self, db: Database, registry: Registry, resource: Any
    ) -> Iterable[InstanceRef]:
        """Every VM uses the singleton ``admin-template:default`` -- the
        admin template defines the admin user on each VM, and there's one
        admin user per VM. No DB column ties VMs to a non-default admin
        template yet (Phase 2a.3 plurified the framework but the operator
        surface still publishes only ``default``). When/if a future SDD
        adds ``[admin_templates.<name>]`` parsing plus a ``vm.admin-template``
        column, this method changes to filter by that column the same way
        the other template kinds do.
        """
        name = resource.name
        if name != "default":
            return
        for vm in db.list_vms():
            yield InstanceRef(instance_kind="vm", instance_name=vm.name)


KIND_REGISTRY["admin-template"] = _AdminTemplateKind()
