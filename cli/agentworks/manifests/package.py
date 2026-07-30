"""The shared, typed-error body for publishing a manifest package.

Both the app-bundled built-in manifests (``agentworks/manifests/builtin/``)
and a system plugin's bundled manifests anchor a directory of YAML resources
inside an importable package, load it through the exact same operator loader
(``load_manifests``, so a bundle cannot drift from operator-manifest
decoding), and publish each decoded entry under a per-file origin. The two
callers differ only in the anchor, the subdirectory, and the origin they
stamp, so the load-and-iterate body lives here once.

Warn-level ``issues`` in a first-party bundle are a curation/app bug, so this
body RAISES a typed ``ConfigError`` the moment a bundle is dirty, rather than
``assert not issues`` (which ``python -O`` strips). A typed raise is also the
correct failure mode for the eventual external-plugin path (the FRD's Future
direction pre-pays this hardening).
"""

from __future__ import annotations

from importlib import resources as importlib_resources
from pathlib import Path
from typing import TYPE_CHECKING

from agentworks.errors import ConfigError
from agentworks.manifests.loader import load_manifests

if TYPE_CHECKING:
    from collections.abc import Callable

    from agentworks.resources.origin import Origin
    from agentworks.resources.registry import Registry


def publish_manifest_package(
    registry: Registry,
    *,
    anchor: str,
    subdir: str,
    origin_for: Callable[[str], Origin],
) -> None:
    """Load the YAML manifests under ``anchor``'s ``subdir`` and publish
    each into ``registry``, stamping ``origin_for(<filename>)`` per entry.

    ``anchor`` is an importlib-resources package anchor (an importable
    package name) and ``subdir`` is the directory beneath it holding the
    ``*.yaml`` (``"builtin"`` for the app bundle, ``"manifests"`` for a
    plugin). ``origin_for`` receives the bundled file's bare name and
    returns the ``Origin`` to stamp (a ``built-in`` source for the app
    bundle, a ``system-plugin`` source for a plugin), so the source string
    points at the actual shipped file.

    An ``anchor`` that does not resolve to an importable package (a typo'd or
    unshipped package name) RAISES ``ConfigError`` naming the anchor/subdir,
    never a raw ``ModuleNotFoundError``/``ImportError``, so a plugin-curation
    bug surfaces as a typed error the caller can attribute (mirroring the
    plugin-attributed re-raises in ``register_plugin``). Warn-level ``issues``
    in a first-party bundle are likewise a curation/app bug: this RAISES
    ``ConfigError`` rather than asserting (``assert`` is stripped under
    ``python -O``), so a dirty bundle fails loudly in every build mode.
    """
    try:
        directory = importlib_resources.files(anchor) / subdir
    except (ImportError, TypeError) as exc:
        # files() raises ModuleNotFoundError/ImportError for an unimportable
        # anchor and TypeError for a non-package module; re-type all of them so
        # a bad manifest anchor is never an opaque traceback.
        raise ConfigError(f"manifest package {anchor!r}/{subdir} could not be resolved: {exc}") from exc
    # The traversable is a real directory both in the repo and in wheels
    # (hatchling ships package data); resolve to a Path for the loader.
    with importlib_resources.as_file(directory) as resolved:
        manifests = load_manifests(Path(resolved))

    if manifests.issues:
        raise ConfigError(f"bundled manifests under {anchor}/{subdir} must be issue-free: {manifests.issues}")

    for entry in manifests.entries:
        registry.add(
            entry.kind,
            entry.name,
            entry.resource,
            origin_for(entry.location.file.name),
        )
