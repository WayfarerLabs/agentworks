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
from agentworks.resources.kind import KIND_REGISTRY

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
    allowed_kinds: frozenset[str] | None = None,
    weak: bool = False,
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

    ``allowed_kinds`` restricts which resource kinds a bundle may declare
    (Phase 7, LLD c 3b.2). ``None`` (the built-in caller's default) permits
    every decoder kind, so ``builtin.py`` is untouched. A plugin caller passes
    the bundleable-kind allowlist (``PLUGIN_MANIFEST_KINDS``): a document whose
    ``kind`` falls outside it raises ``ConfigError`` naming the kind and file,
    and a document whose ``name`` is in the kind's reserved
    ``auto_declare_names`` set (a plugin bundling a ``default`` template) raises
    ``ConfigError`` naming the kind, reserved name, and file, so a plugin cannot
    shadow the framework's auto-declared default. The caller
    (``_publish_plugin_manifests``) re-raises both with plugin attribution.

    ``weak`` (Phase 7, LLD c 3b.3) is forwarded to every ``registry.add``: a
    not-enabled plugin's manifest rows publish weak (add-if-absent) so they
    never block a stronger row; the built-in caller and enabled plugins publish
    strong (the default).

    An ``anchor`` that does not resolve to an importable package (a typo'd or
    unshipped package name) RAISES ``ConfigError`` naming the anchor/subdir,
    never a raw ``ModuleNotFoundError``/``ImportError``, so a plugin-curation
    bug surfaces as a typed error the caller can attribute (mirroring the
    plugin-attributed re-raises in ``register_plugin``). A plugin
    (``allowed_kinds is not None``) whose anchor imports but ships no ``subdir``
    directory likewise RAISES ``ConfigError`` naming the anchor/subdir, rather
    than falling through to ``load_manifests`` on a non-existent directory and
    publishing nothing silently (a plugin declared ``manifests`` but bundled no
    package data). The built-in caller (``allowed_kinds=None``, its ``builtin/``
    subdir always ships) is never gated by this check. Warn-level ``issues``
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
    # A plugin (``allowed_kinds is not None``) that imports fine but ships no
    # ``<subdir>/`` package data is a curation bug: the anchor resolved, so the
    # unimportable-anchor guard above never fired, but ``load_manifests`` on the
    # missing directory would silently publish nothing. Fail loudly instead. The
    # ``.is_dir()`` traversable check reads False for a missing subdir under both
    # the repo (a real ``PosixPath``) and a wheel install (a ``zipfile.Path``),
    # so it holds for every install mode. The built-in caller passes
    # ``allowed_kinds=None`` and its ``builtin/`` subdir always ships, so it is
    # never gated here and its behavior is unchanged.
    if allowed_kinds is not None and not directory.is_dir():
        raise ConfigError(
            f"plugin manifest package {anchor!r} declares manifests but ships no {subdir!r} "
            f"package data (expected a {subdir}/ directory beside {anchor})"
        )
    # The traversable is a real directory both in the repo and in wheels
    # (hatchling ships package data); resolve to a Path for the loader.
    with importlib_resources.as_file(directory) as resolved:
        manifests = load_manifests(Path(resolved))

    if manifests.issues:
        raise ConfigError(f"bundled manifests under {anchor}/{subdir} must be issue-free: {manifests.issues}")
    # Deprecated shapes in a first-party bundle are the same class of
    # curation bug: shipped manifests are the pattern book operators
    # copy, so they must always spell the canonical shape.
    if manifests.deprecation_issues:
        raise ConfigError(
            f"bundled manifests under {anchor}/{subdir} must not use deprecated shapes: {manifests.deprecation_issues}"
        )

    for entry in manifests.entries:
        file_name = entry.location.file.name
        if allowed_kinds is not None:
            _reject_unbundleable(entry.kind, entry.name, file_name, allowed_kinds)
        registry.add(
            entry.kind,
            entry.name,
            entry.resource,
            origin_for(file_name),
            weak=weak,
        )


def _reject_unbundleable(kind: str, name: str, file_name: str, allowed_kinds: frozenset[str]) -> None:
    """Guard a plugin-bundled ``(kind, name)`` against the two ways a bundle can
    subvert the enablement guarantee (Phase 7, LLD c 3b.2): a kind outside the
    gated allowlist, or a reserved auto-declared name.

    A kind not in ``allowed_kinds`` has no wired consumption gate, so a disabled
    plugin's row of it would be silently usable. A name in the kind's
    ``auto_declare_names`` (the templates' reserved ``default``) would land in
    the free reserved slot and BECOME the framework default: disabled, it gates
    every implicit-default use; enabled, it silently shadows the framework's
    own default, and no collision fires to catch it. Both are rejected at
    publish. Raises naming the kind/name and file; the caller attributes the
    plugin.
    """
    if kind not in allowed_kinds:
        raise ConfigError(
            f"kind {kind!r} (in {file_name}) is not a bundleable manifest kind; "
            f"a plugin may only bundle: {', '.join(sorted(allowed_kinds))}"
        )
    handler = KIND_REGISTRY.get(kind)
    reserved = handler.auto_declare_names if handler is not None else None
    if reserved is not None and name in reserved:
        raise ConfigError(
            f"{kind} {name!r} (in {file_name}) is a reserved auto-declared name; "
            f"a plugin may not bundle it (it would shadow the framework's default {kind})"
        )
