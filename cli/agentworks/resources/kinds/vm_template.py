"""``VMTemplateKind``: framework strategy for the ``"vm_template"`` kind.

Miss policy ``auto-declare`` with reserved name ``"default"`` -- the
framework synthesizes ``vm_template:default`` (and only ``"default"``)
when no operator declaration covers it. Any other missing name (a typo
in ``inherits = ["defualt"]`` etc.) surfaces as a framework miss-policy
error with the requirement source attached. Cycle detection across
``inherits`` chains runs uniformly via the registry's cycle pass.

Per-template field-merging stays in ``agentworks.vms.templates``: the
framework owns reference validation; the resolver owns inheritance
semantics. ``synthesize`` returns a code-defined default ``VMTemplate``
(all optional fields ``None`` per VMTemplate's inherit shape; the
resolver layers concrete defaults from ``ResolvedVMTemplate`` on top).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from agentworks.config import VMTemplate
from agentworks.resources.kind import ALWAYS_MATERIALIZE_SOURCE, KIND_REGISTRY
from agentworks.resources.origin import Origin

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agentworks.resources.requirement import ResourceRequirement


@dataclass(frozen=True)
class _VMTemplateKind:
    """Implementation of ``ResourceKind`` for ``"vm_template"``."""

    kind: str = "vm_template"
    miss_policy: Literal["auto-declare", "error"] = "auto-declare"
    auto_declare_names: frozenset[str] | None = frozenset({"default"})

    def synthesize(self, requirements: Sequence[ResourceRequirement]) -> VMTemplate:
        """Build a code-defined default ``VMTemplate``.

        Returns the kind's baseline: ``VMTemplate(name="default")`` with
        all optional fields at their inherit-shaped defaults (``None`` /
        empty). The VM-template resolver in ``agentworks.vms.templates``
        merges this with any inheriting templates and layers concrete
        defaults via ``ResolvedVMTemplate``.

        Tolerates ``requirements=()`` (the always-materialize pre-step's
        path): synthesizes with the reserved
        ``("framework", "always-materialize")`` source so the
        breadcrumb shows where the row came from. With non-empty
        ``requirements`` (the worklist-driven path -- a child template's
        ``inherits = ["default"]`` triggers auto-declare when no
        operator declaration exists), the first requirement's source is
        recorded as origin.
        """
        source = requirements[0].source if requirements else ALWAYS_MATERIALIZE_SOURCE
        return VMTemplate(name="default", origin=Origin.auto_declared(source=source))


KIND_REGISTRY["vm_template"] = _VMTemplateKind()
