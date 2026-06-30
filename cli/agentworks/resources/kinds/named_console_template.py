"""``NamedConsoleTemplateKind``: the framework's strategy for the
``"named_console_template"`` kind.

Operator-surface-singleton today: ``Config.publish_to`` always publishes
``named_console_template:default`` (even when no ``[named_console]``
section exists), so the auto-declare path is a safety net for typo'd
references rather than a routine occurrence. Phase 2a.3 plurified
``admin_template`` at the framework level (matching the other template
kinds) but deliberately scoped that change to admin only; this kind
follows when there's an operator need to declare named console
templates. The kind shape (``auto_declare_names = {"default"}``,
synthesize tolerating ``requirements=()``) is already aligned with the
named-multi-instance template kinds, so plurifying the operator surface
later is a parser/loader change, not a framework change.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from agentworks.config import NamedConsoleConfig
from agentworks.resources.kind import ALWAYS_MATERIALIZE_SOURCE, KIND_REGISTRY
from agentworks.resources.origin import Origin

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agentworks.resources.reference import ResourceReference


@dataclass(frozen=True)
class _NamedConsoleTemplateKind:
    """Implementation of ``ResourceKind`` for ``"named_console_template"``."""

    kind: str = "named_console_template"
    miss_policy: Literal["auto-declare", "error"] = "auto-declare"
    auto_declare_names: frozenset[str] | None = frozenset({"default"})

    def synthesize(
        self,
        requirements: Sequence[ResourceReference],
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
