"""App-bundled built-in resource manifests.

Resources the app ships as data (rather than as code publishers) live in
``agentworks/manifests/builtin/`` and go through the exact same loader
as operator manifests, landing with ``Origin.built_in``. The bundle's
first content is ``vm-sites.yaml`` (the reserved ``lima`` / ``wsl2``
sites); future built-ins and plugins (their own origin variants) are the
mechanism's further consumers.
"""

from __future__ import annotations

from importlib import resources as importlib_resources
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from agentworks.manifests.loader import ManifestEntry
    from agentworks.resources.registry import Registry

_BUILTIN_SOURCE = "agentworks.manifests.builtin"


def publish_to(
    registry: Registry,
    *,
    skip: Callable[[ManifestEntry], bool] | None = None,
) -> None:
    """Publish every bundled manifest with a ``built-in`` origin.

    The origin's source carries the bundled filename
    (``agentworks.manifests.builtin/<filename>``) so ``agw resource
    describe`` points at the actual shipped file. Bundled manifests are
    app data: warn-level issues in them are app bugs, asserted here so
    CI catches a dirty bundle the moment content is added.

    ``skip`` is the app composition's host-conditional filter: this
    module stays a dumb bundle loader, and decisions like "bundled
    vm-sites publish only for platforms enabled on this host" belong to
    ``bootstrap.build_registry``, which composes the predicate from
    config. ``None`` publishes everything.
    """
    from agentworks.manifests.loader import load_manifests
    from agentworks.resources import Origin

    bundle = importlib_resources.files("agentworks.manifests") / "builtin"
    # The traversable is a real directory both in the repo and in wheels
    # (hatchling ships package data); resolve to a Path for the loader.
    with importlib_resources.as_file(bundle) as bundle_dir:
        manifests = load_manifests(Path(bundle_dir))

    assert not manifests.issues, (
        f"bundled manifests must be issue-free: {manifests.issues}"
    )
    for entry in manifests.entries:
        if skip is not None and skip(entry):
            continue
        registry.add(
            entry.kind,
            entry.name,
            entry.resource,
            Origin.built_in(
                source=f"{_BUILTIN_SOURCE}/{entry.location.file.name}"
            ),
        )
