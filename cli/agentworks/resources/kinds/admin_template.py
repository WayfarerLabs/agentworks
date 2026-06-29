"""``AdminTemplateKind``: the framework's strategy for the
``"admin_template"`` kind.

Phase 2a.3 plurified this kind from singleton-conceptual to
named-multi-instance, matching the shape of ``vm_template`` /
``agent_template`` / ``workspace_template`` / ``session_template``. The
operator-facing surface is unchanged in Phase 2a: the loader still only
accepts the singleton ``[admin]`` block and publishes one
``admin_template:default`` row. A future SDD adds
``[admin_templates.<name>]`` parsing, the ``--admin-template`` CLI flag,
and the VM DB column without re-touching the framework.

Miss policy is ``auto-declare`` with reserved name ``"default"`` -- the
always-materialize pre-step seeds ``admin_template:default`` when it
isn't already published (Config publishes the empty-defaults instance
even when no ``[admin.*]`` sections exist, so the pre-step usually
short-circuits). Typo'd names like ``admin_template:custom`` referenced
from somewhere error via the framework's miss-policy dispatch.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from agentworks.config import AdminConfig
from agentworks.resources.kind import ALWAYS_MATERIALIZE_SOURCE, KIND_REGISTRY
from agentworks.resources.origin import Origin

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agentworks.resources.requirement import ResourceRequirement


@dataclass(frozen=True)
class _AdminTemplateKind:
    """Implementation of ``ResourceKind`` for ``"admin_template"``."""

    kind: str = "admin_template"
    miss_policy: Literal["auto-declare", "error"] = "auto-declare"
    auto_declare_names: frozenset[str] | None = frozenset({"default"})

    def synthesize(self, requirements: Sequence[ResourceRequirement]) -> AdminConfig:
        """Build an empty-defaults ``AdminConfig`` for an auto-declared
        ``admin_template:default``. In practice ``Config.publish_to``
        always publishes a real one before ``finalize`` runs, so the
        always-materialize pre-step short-circuits and this call is
        rare. See ``vm_template.py``'s ``synthesize`` for the rationale
        on why the non-empty-``requirements`` path is preserved.
        """
        source = requirements[0].source if requirements else ALWAYS_MATERIALIZE_SOURCE
        return AdminConfig(name="default", origin=Origin.auto_declared(source=source))


KIND_REGISTRY["admin_template"] = _AdminTemplateKind()
