"""Framework strategy for the ``secret-config`` kind: the singleton
active-backend-chain row.

``[secret_config]`` stays pure config (TOML is its only home; the kind
is not manifest-declarable), but its ``backends`` list names resources,
so ``Config.publish_to`` publishes one ``secret-config:default`` row
whose ``referenced_resources()`` emits a ``secret-backend`` edge per
chain entry. That collapses chain-name validation into the framework's
error miss policy and makes the chain visible in ``agw resource list``.

Miss policy is ``auto-declare`` with reserved name ``"default"``, same
shape as ``admin-template``: ``Config.publish_to`` always publishes the
real row (the loader defaults absent ``[secret_config]`` to the built-in
chain), so ``synthesize`` fires only for registries built without a
config publisher (hand-built test registries). The synthesized sentinel
carries an EMPTY chain -- no edges, no semantic checks -- so such
registries finalize cleanly without secret plumbing.

The ``validate(registry)`` hook is where the secret system's semantic
check lives: every operator-declared secret must be reachable via the
active chain. Delegated to ``agentworks.secrets.providers``, which owns
the provider/source knowledge.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from agentworks.resources.kind import ALWAYS_MATERIALIZE_SOURCE, KIND_REGISTRY
from agentworks.resources.origin import Origin

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agentworks.resources.reference import ResourceReference
    from agentworks.resources.registry import Registry
    from agentworks.secrets.base import SecretConfig


@dataclass(frozen=True)
class _SecretConfigKind:
    """Implementation of ``ResourceKind`` for ``"secret-config"``."""

    kind: str = "secret-config"
    miss_policy: Literal["auto-declare", "error"] = "auto-declare"
    auto_declare_names: frozenset[str] | None = frozenset({"default"})
    manifest_declarable: bool = False
    builtin_override: Literal["allow", "reserved"] = "reserved"

    def synthesize(self, references: Sequence[ResourceReference]) -> SecretConfig:
        """The empty-chain sentinel for registries without a config
        publisher. Deliberately NOT the built-in default chain: the
        sentinel must emit no edges (hand-built registries carry no
        backend rows) and ``validate`` skips it. Real configs never
        reach here -- ``Config.publish_to`` always publishes the loaded
        (or loader-defaulted) row first.
        """
        from agentworks.secrets.base import SecretConfig

        source = references[0].source if references else ALWAYS_MATERIALIZE_SOURCE
        return SecretConfig(
            backends=(), origin=Origin.auto_declared(source=source)
        )

    def validate(self, registry: Registry) -> None:
        """Finalize-time semantic check: chain sources instantiate and
        every operator-declared secret is reachable. Skipped for the
        empty-chain sentinel (no config publisher, nothing to check).
        """
        row = registry.lookup("secret-config", "default")
        origin = getattr(row, "origin", None)
        if (
            origin is not None
            and origin.variant == "auto-declared"
            and origin.source == ALWAYS_MATERIALIZE_SOURCE
        ):
            return

        from agentworks.secrets.providers import validate_chain

        validate_chain(registry)


KIND_REGISTRY["secret-config"] = _SecretConfigKind()
