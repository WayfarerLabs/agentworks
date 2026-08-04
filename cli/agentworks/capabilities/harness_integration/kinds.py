"""``_HarnessIntegrationKind``: the framework strategy for the ``"harness-integration"`` kind,
plus the ``HarnessIntegrationEntry`` capability row.

Lives in the ``capabilities.harness_integration`` package next to the harness integration
implementations; ``agentworks.resources.kinds.__init__`` imports this
module so the kind self-registers into ``KIND_REGISTRY`` at load.

``_HarnessIntegrationKind`` gives the framework a name-keyed marker so a
``session-template`` ``spec.harness_integration`` value typo surfaces uniformly. The
harness integration implementations live in ``agentworks.capabilities.harness_integration``; the
companion publisher there adds one ``HarnessIntegrationEntry`` row per known
harness integration, built-in with source ``"agentworks.capabilities.harness_integration"``. It
mirrors ``_GitCredentialProviderKind`` exactly (``category="capability"``,
``miss_policy="error"``, ``builtin_override="reserved"``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from agentworks.resources.kind import KIND_REGISTRY, NoUnreferencedDefaultError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agentworks.resources.origin import Origin
    from agentworks.resources.reference import ResourceReference


@dataclass(frozen=True)
class HarnessIntegrationEntry:
    """A name-keyed marker for one harness integration capability (``"shell"``,
    ``"claude-code"``).

    The actual harness integration class (``ShellIntegration``, ``ClaudeCodeIntegration``)
    lives beside this in ``agentworks.capabilities.harness_integration``; this row is
    what a ``session-template`` ``spec.harness_integration`` reference resolves
    against in the framework.

    Inbound references live on the dependency graph
    (``Registry.graph.dependents_of``), not on this row.
    """

    name: str
    origin: Origin | None = None


@dataclass(frozen=True)
class _HarnessIntegrationKind:
    """Implementation of ``ResourceKind`` for ``"harness-integration"``."""

    kind: str = "harness-integration"
    description: str = "Capability for running a session workload (shell, claude-code)"
    miss_policy: Literal["auto-declare", "error"] = "error"
    auto_declare_names: frozenset[str] | None = None
    category: Literal["declarable", "capability"] = "capability"
    builtin_override: Literal["allow", "reserved"] = "reserved"

    def synthesize(self, references: Sequence[ResourceReference]) -> Any:
        # Unreachable under the error miss policy; honors the
        # empty-references contract via the typed framework error so a
        # future change that gives the kind a reserved default has an
        # obvious landing pad.
        raise NoUnreferencedDefaultError(
            "the harness integration kind has miss_policy='error'; synthesize should "
            "never be invoked (the framework raises ConfigError first)"
        )


KIND_REGISTRY["harness-integration"] = _HarnessIntegrationKind()
