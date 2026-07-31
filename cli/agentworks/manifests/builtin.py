"""App-bundled built-in resource manifests.

Resources the app ships as data (rather than as code publishers) live in
``agentworks/manifests/builtin/`` and go through the exact same loader
as operator manifests, landing with ``Origin.built_in``. The bundle's
first content is ``vm-sites.yaml`` (the reserved ``lima-local`` /
``wsl2`` sites); future built-ins and plugins (their own origin
variants) are the mechanism's further consumers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agentworks.resources.registry import Registry

_BUILTIN_SOURCE = "agentworks.manifests.builtin"


def publish_to(registry: Registry) -> None:
    """Publish every bundled manifest with a ``built-in`` origin,
    unconditionally: host suitability is the resource's own concern
    (a bundled vm-site registers everywhere and self-disables where it
    lacks what it needs), never a publish-time filter.

    The origin's source carries the bundled filename
    (``agentworks.manifests.builtin/<filename>``) so ``agw resource
    describe`` points at the actual shipped file. Bundled manifests are
    app data: warn-level issues in them are app bugs, so the shared
    ``publish_manifest_package`` body raises a typed error the moment a
    bundle is dirty (a raise, not an ``assert``, so it holds under
    ``python -O``).
    """
    from agentworks.manifests.package import publish_manifest_package
    from agentworks.resources import Origin

    publish_manifest_package(
        registry,
        anchor="agentworks.manifests",
        subdir="builtin",
        origin_for=lambda file_name: Origin.built_in(source=f"{_BUILTIN_SOURCE}/{file_name}"),
    )
