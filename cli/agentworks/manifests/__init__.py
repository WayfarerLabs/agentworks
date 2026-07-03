"""YAML resource manifests: the operator publisher (resource-manifests SDD).

Operators declare resources as Kubernetes-style YAML documents under the
resources directory (``<config-dir>/resources/``), auto-loaded on every
invocation. This package owns the walk, the envelope, the spec decode
(which reuses the TOML loaders' validation verbatim so the two sources
cannot drift while both exist), and the publisher.

Public surface: ``load_manifests``, ``ManifestSet``,
``RESOURCES_DIRNAME``, and ``builtin`` (the app-bundled manifests
publisher).
"""

from __future__ import annotations

from agentworks.manifests.loader import (
    RESOURCES_DIRNAME,
    ManifestEntry,
    ManifestSet,
    load_manifests,
)

__all__ = [
    "RESOURCES_DIRNAME",
    "ManifestEntry",
    "ManifestSet",
    "load_manifests",
]
