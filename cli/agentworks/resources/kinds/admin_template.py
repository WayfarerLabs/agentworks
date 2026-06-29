"""``AdminTemplateKind``: the framework's strategy for the
``"admin_template"`` singleton-backed kind.

The admin Resource is a singleton in the operator's TOML schema
(``[admin.config]``, ``[admin.env]``, etc., with no explicit ``[admin]``
header). The Registry models it as a one-row kind with reserved name
``"default"`` so it appears in ``agw resource list``, can be the source of
auto-declared secrets via its env block, and routes through framework
dispatch uniformly with the multi-named template kinds.

Miss policy is ``auto-declare`` with reserved name ``"default"`` -- a
safety net, because ``Config.publish_to`` always publishes
``admin_template:default`` (even when no ``[admin.*]`` sections exist,
Config publishes an empty-defaults instance). Pinning auto-declare keeps
the framework-dispatch shape uniform with the Phase 2a template kinds and
errors loudly on typo'd names like ``admin_template:custom``.
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
        always-materialize pre-step short-circuits and this call is rare.

        Tolerates ``requirements=()`` per the Phase 2a contract: the
        framework's always-materialize pre-step calls it that way for
        kinds with a non-None ``auto_declare_names`` set. With no
        incoming requirement, the synthetic
        ``("framework", "always-materialize")`` source is used so the
        breadcrumb shows where the row came from.
        """
        source = requirements[0].source if requirements else ALWAYS_MATERIALIZE_SOURCE
        return AdminConfig(origin=Origin.auto_declared(source=source))


KIND_REGISTRY["admin_template"] = _AdminTemplateKind()
