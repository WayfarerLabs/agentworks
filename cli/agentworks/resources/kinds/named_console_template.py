"""``NamedConsoleTemplateKind``: the framework's strategy for the
``"named_console_template"`` singleton-backed kind.

Same shape as ``admin_template``: ``Config.publish_to`` always publishes
``named_console_template:default`` (even when no ``[named_console]``
section exists), so the auto-declare path is a safety net for typo'd
references rather than a routine occurrence.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from agentworks.config import NamedConsoleConfig
from agentworks.resources.kind import ALWAYS_MATERIALIZE_SOURCE, KIND_REGISTRY
from agentworks.resources.origin import Origin

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agentworks.resources.requirement import ResourceRequirement


@dataclass(frozen=True)
class _NamedConsoleTemplateKind:
    """Implementation of ``ResourceKind`` for ``"named_console_template"``."""

    kind: str = "named_console_template"
    miss_policy: Literal["auto-declare", "error"] = "auto-declare"
    auto_declare_names: frozenset[str] | None = frozenset({"default"})

    def synthesize(
        self,
        requirements: Sequence[ResourceRequirement],
    ) -> NamedConsoleConfig:
        """Build an empty-defaults ``NamedConsoleConfig`` for an
        auto-declared ``named_console_template:default``. Same shape as
        the admin template kind: ``Config.publish_to`` always publishes
        a real one so the always-materialize pre-step short-circuits.

        Tolerates ``requirements=()`` per the Phase 2a contract; uses
        the synthetic ``("framework", "always-materialize")`` source
        when called that way.
        """
        source = requirements[0].source if requirements else ALWAYS_MATERIALIZE_SOURCE
        return NamedConsoleConfig(origin=Origin.auto_declared(source=source))


KIND_REGISTRY["named_console_template"] = _NamedConsoleTemplateKind()
