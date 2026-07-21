"""``agentworks.resources``: the Resource Registry framework.

The Registry is the framework's typed, queryable Resource store. Publishers
(``agentworks.config``'s ``Config.publish_to``, the ``agentworks.apt`` /
``agentworks.install_commands`` operator publishers, the bundled built-in
manifests, future plugins / YAML manifest publishers) push composed
Resources into it; ``Registry.finalize()`` runs the framework pass that walks
the reference graph, dispatches miss policies (potentially synthesizing
auto-declared Resources), attaches ``usage`` lists, detects cycles, and
freezes the Registry.

For the standard case, ``agentworks.bootstrap.build_registry`` orchestrates
the full set of publishers. The lower-level ``Registry.empty()`` / ``add`` /
``finalize`` triad is exposed for tests and multi-source orchestration.
"""

from __future__ import annotations

# Importing .kinds populates KIND_REGISTRY at module-load time via each kind
# module's self-registration. New kinds slot in by adding new files
# under kinds/ and importing them from kinds/__init__.py.
from agentworks.resources import kinds  # noqa: F401
from agentworks.resources.kind import (
    ALWAYS_MATERIALIZE_SOURCE,
    KIND_REGISTRY,
    InstanceRef,
    NoUnreferencedDefaultError,
    ResourceKind,
)
from agentworks.resources.origin import Origin
from agentworks.resources.reference import (
    ReferenceEntry,
    ResourceReference,
    SecretReference,
    TemplateReference,
)
from agentworks.resources.registry import Registry
from agentworks.resources.walk import collect_secrets_for

__all__ = [
    "ALWAYS_MATERIALIZE_SOURCE",
    "KIND_REGISTRY",
    "InstanceRef",
    "NoUnreferencedDefaultError",
    "Origin",
    "Registry",
    "ResourceKind",
    "ResourceReference",
    "SecretReference",
    "TemplateReference",
    "ReferenceEntry",
    "collect_secrets_for",
]
