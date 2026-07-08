"""Kind registrations for the vms domain: ``vm-template`` and
``admin-template`` (the admin user is a per-VM concept -- provisioned by
``vms/initializer``, one per VM; its ``instances()`` iterates VMs).

Lives in the ``vms`` domain package next to the code that implements VM
templates; ``agentworks.resources.kinds.__init__`` imports this module so
the kind self-registers into ``KIND_REGISTRY`` at load.

Miss policy ``auto-declare`` with reserved name ``"default"`` -- the
framework synthesizes ``vm-template:default`` (and only ``"default"``)
when no operator declaration covers it. Any other missing name (a typo
in ``inherits = ["defualt"]`` etc.) surfaces as a framework miss-policy
error with the reference source attached. Cycle detection across
``inherits`` chains runs uniformly via the registry's cycle pass.

Per-template field-merging stays in ``agentworks.vms.templates``: the
framework owns reference validation; the resolver owns inheritance
semantics. ``synthesize`` returns a code-defined default ``VMTemplate``
(all optional fields ``None`` per VMTemplate's inherit shape; the
resolver layers concrete defaults from ``ResolvedVMTemplate`` on top).
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
from agentworks.vms.admin import AdminConfig
from agentworks.vms.template import VMTemplate

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from agentworks.db import Database
    from agentworks.resources.reference import ResourceReference
    from agentworks.resources.registry import Registry


@dataclass(frozen=True)
class _VMTemplateKind:
    """Implementation of ``ResourceKind`` for ``"vm-template"``."""

    kind: str = "vm-template"
    description: str = "VM sizing/provisioning templates for agw vm create"
    miss_policy: Literal["auto-declare", "error"] = "auto-declare"
    auto_declare_names: frozenset[str] | None = frozenset({"default"})
    category: Literal["declarable", "capability"] = "declarable"
    builtin_override: Literal["allow", "reserved"] = "reserved"

    def synthesize(self, references: Sequence[ResourceReference]) -> VMTemplate:
        """Build a code-defined default ``VMTemplate``.

        Returns the kind's baseline: ``VMTemplate(name="default")`` with
        all optional fields at their inherit-shaped defaults (``None`` /
        empty). The VM-template resolver in ``agentworks.vms.templates``
        merges this with any inheriting templates and layers concrete
        defaults via ``ResolvedVMTemplate``.

        Tolerates ``references=()`` (the always-materialize pre-step's
        path): synthesizes with the reserved
        ``("framework", "always-materialize")`` source so the
        breadcrumb shows where the row came from. This is the only path
        the framework actually takes for VMTemplateKind today: the
        always-materialize pre-step seeds ``vm-template:default`` before
        the worklist loop, so by the time any child reference is
        dispatched the target is a hit, not a miss. The non-empty path
        is kept for symmetry with other kinds and to keep the door open
        for future cases (e.g. operator-declared kinds whose default
        isn't always-materialized).
        """
        source = references[0].source if references else ALWAYS_MATERIALIZE_SOURCE
        return VMTemplate(name="default", origin=Origin.auto_declared(source=source))

    def instances(
        self, db: Database, registry: Registry, resource: Any
    ) -> Iterable[InstanceRef]:
        """Every VM whose ``template`` column matches this VMTemplate's
        name -- or whose ``template`` is NULL when the resource is the
        reserved ``default`` (a NULL ``template`` column means "use the
        framework's default template at provisioning time").
        """
        name = resource.name
        for vm in db.list_vms():
            if vm.template == name or (vm.template is None and name == "default"):
                yield InstanceRef(instance_kind="vm", instance_name=vm.name)


KIND_REGISTRY["vm-template"] = _VMTemplateKind()


@dataclass(frozen=True)
class _AdminTemplateKind:
    """Implementation of ``ResourceKind`` for ``"admin-template"``."""

    kind: str = "admin-template"
    description: str = "Admin user environment on VMs (only 'default' selectable today)"
    miss_policy: Literal["auto-declare", "error"] = "auto-declare"
    auto_declare_names: frozenset[str] | None = frozenset({"default"})
    category: Literal["declarable", "capability"] = "declarable"
    builtin_override: Literal["allow", "reserved"] = "reserved"

    def synthesize(self, references: Sequence[ResourceReference]) -> AdminConfig:
        """Build an empty-defaults ``AdminConfig`` for an auto-declared
        ``admin-template:default``.

        Rarely actually called: ``Config.publish_to`` always publishes a
        real ``admin-template:default`` from ``Config.admin`` (even when
        the operator omits every ``[admin.*]`` section -- the loader
        synthesizes an empty-defaults instance), so the always-materialize
        pre-step's "is the name already in the registry?" short-circuits
        before reaching this method. See ``agentworks.vms.kinds``'s
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
