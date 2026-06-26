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
from agentworks.resources.kind import KIND_REGISTRY
from agentworks.resources.origin import Origin
from agentworks.resources.requirement import UsageEntry

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
        auto-declared ``named_console_template:default``.
        """
        first = requirements[0]
        return NamedConsoleConfig(
            origin=Origin.auto_declared(source=first.source),
            usage=tuple(UsageEntry(source=r.source, text=r.usage) for r in requirements),
        )


KIND_REGISTRY["named_console_template"] = _NamedConsoleTemplateKind()
