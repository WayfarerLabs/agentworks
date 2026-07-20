"""``_HarnessKind``: the framework strategy for the ``"harness"`` kind,
plus the ``HarnessEntry`` capability row.

Lives in the ``capabilities.harness`` package next to the harness
implementations; ``agentworks.resources.kinds.__init__`` imports this
module so the kind self-registers into ``KIND_REGISTRY`` at load.

``_HarnessKind`` gives the framework a name-keyed marker so a
``session-template`` ``spec.harness`` value typo surfaces uniformly. The
harness implementations live in ``agentworks.capabilities.harness``; the
companion publisher there adds one ``HarnessEntry`` row per known
harness, built-in with source ``"agentworks.capabilities.harness"``. It
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
    from agentworks.resources.reference import ReferenceEntry, ResourceReference


@dataclass(frozen=True)
class HarnessEntry:
    """A name-keyed marker for one harness capability (``"shell"``,
    ``"claude-code"``).

    The actual harness class (``ShellHarness``, ``ClaudeCodeHarness``)
    lives beside this in ``agentworks.capabilities.harness``; this row is
    what a ``session-template`` ``spec.harness`` reference resolves
    against in the framework.
    """

    name: str
    origin: Origin | None = None
    references: tuple[ReferenceEntry, ...] = ()


@dataclass(frozen=True)
class _HarnessKind:
    """Implementation of ``ResourceKind`` for ``"harness"``."""

    kind: str = "harness"
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
            "the harness kind has miss_policy='error'; synthesize should "
            "never be invoked (the framework raises ConfigError first)"
        )


KIND_REGISTRY["harness"] = _HarnessKind()
