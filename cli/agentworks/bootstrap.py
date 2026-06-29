"""Application-level glue: assemble a finalized ``Registry`` from the
standard set of publishers.

The "standard set of publishers" -- today, just ``Config``; from Phase 2b,
also ``agentworks.catalog`` -- is application knowledge, not Registry
knowledge and not Config knowledge. This module is its legitimate home: it
imports both ``Config`` and ``Registry`` (and, when Phase 2b lands, the
catalog publisher) and orchestrates them. Registry stays publisher-
agnostic; Config stays unaware of catalog.

Call sites that need a finalized Registry for the common Config case use
``build_registry(config)``. Tests and multi-source orchestration can
assemble Registry by hand with ``Registry.empty()`` + explicit
``publish_to`` calls + ``finalize``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks.resources import Registry

if TYPE_CHECKING:
    from agentworks.config import Config


def build_registry(config: Config) -> Registry:
    """Build a finalized ``Registry`` from the standard set of publishers.

    Publisher order: code-declared publishers first (``catalog``,
    ``git_credentials``, ``secrets``), then ``Config.publish_to``
    (operator-declared resources, which can re-publish any
    code-declared ``(kind, name)`` with operator-declared Origin to
    override). Future publishers (plugins, YAML manifests, ...) join
    the same sequence by being added here.
    """
    from agentworks import catalog, git_credentials, secrets

    registry = Registry.empty()
    catalog.publish_to(registry)
    git_credentials.publish_to(registry)
    secrets.publish_to(registry)
    config.publish_to(registry)
    registry.finalize()
    return registry
