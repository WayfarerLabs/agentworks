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

    Phase 1a runs only ``Config.publish_to``. Phase 2b extends this to
    run ``catalog.publish_to(registry)`` first so any operator-declared
    override of catalog entries (not supported today, but the order
    keeps the door open) layers on top of the code-declared base.
    Future publishers (plugins, YAML manifests, ...) join the
    same sequence by being added here.
    """
    registry = Registry.empty()
    # Phase 2b: catalog.publish_to(registry) goes here, before
    # config.publish_to, so config can override catalog-declared
    # Resources by re-publishing with operator-declared Origin.
    config.publish_to(registry)
    registry.finalize()
    return registry
