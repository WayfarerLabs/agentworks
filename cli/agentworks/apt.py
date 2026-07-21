"""Apt sources and packages: the two apt declarable resource kinds.

Two first-class Registry kinds live here next to the code that loads them:

- ``apt-source`` (``AptSourceEntry``): a 3rd-party apt repository (key +
  source-list stanza).
- ``apt-package`` (``AptPackageEntry``): a named apt package, optionally
  tied to one or more ``apt-source`` names via ``apt_sources``.

Both are ``declarable`` kinds under the ``error`` miss policy: a typo'd
reference (an unknown apt-source named by a package, or an unknown
apt-package named by a vm-template) surfaces as a framework
``ConfigError`` at ``build_registry`` time citing the reference's source.
Built-in entries ship as bundled manifests under ``manifests/builtin/``;
operators may add or override entries via YAML manifests (or the
deprecated TOML surface, published by ``publish_to`` below).

``agentworks.resources.kinds.__init__`` imports this module so the two
kinds self-register into ``KIND_REGISTRY`` at load.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from agentworks.declared_resource import DeclaredResource
from agentworks.errors import ConfigError
from agentworks.resource_loading import (
    _SYNTHESIZED_DECLS,
    _require_field,
    _require_list,
)
from agentworks.resources.kind import KIND_REGISTRY, synthesize_no_default

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agentworks.config import Config, _SectionLineMap
    from agentworks.resources import Registry
    from agentworks.resources.reference import ResourceReference


# -- Data classes --------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class AptSourceEntry(DeclaredResource):
    """One apt repository source. Referenced by ``AptPackageEntry.apt_sources``
    when a package requires a source's key + list stanza before it can be
    installed. A first-class, system-declared Registry Resource: inherits the
    uniform metadata from ``DeclaredResource`` (the publisher stamps
    ``origin`` as ``built-in`` for shipped entries or ``operator-declared`` for
    config-added ones; ``references`` is attached by the framework's finalize
    pass from the apt_packages that name it).
    """

    # Required (the base makes it optional); see SecretDecl for why field().
    description: str = field()
    key_url: str
    key_path: str
    source: str
    source_file: str
    key_dearmor: bool = False


@dataclass(frozen=True, kw_only=True)
class AptPackageEntry(DeclaredResource):
    # First-class, system-declared Registry citizen; the uniform metadata
    # (name, origin, references, ...) comes from ``DeclaredResource``.
    # ``description`` is required here (see field() note on
    # ``AptSourceEntry``).
    description: str = field()
    apt: list[str]
    apt_sources: list[str] = field(default_factory=list)

    def referenced_resources(self) -> list[ResourceReference]:
        """Emit one ``ResourceReference`` per name in ``apt_sources``. The
        framework's ``apt-source`` kind uses an ``error`` miss policy, so
        an unknown source name surfaces as a clean ``ConfigError`` at
        ``build_registry`` time with the referencing package's identity
        attached (rather than the pre-Phase-2b silent ordering assumption
        that packages must appear after their sources in TOML).

        The registry attaches the corresponding ``ReferenceEntry`` to
        each ``AptSourceEntry`` during finalize, so
        ``agw resource describe apt-source/github`` shows every apt-package
        that depends on it: the dependency graph that was previously
        implicit is now visible.
        """
        from agentworks.resources.reference import ResourceReference

        return [
            ResourceReference(
                name=source_name,
                kind="apt-source",
                usage=f"the {source_name} apt source",
                source=("apt-package", self.name),
            )
            for source_name in self.apt_sources
        ]


# -- Loading -------------------------------------------------------------------

# source_file must be a simple filename (no slashes, no shell metacharacters)
_SAFE_FILENAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")


def _load_apt_sources(
    raw: dict[str, object],
    decls: _SectionLineMap = _SYNTHESIZED_DECLS,
) -> dict[str, AptSourceEntry]:
    entries: dict[str, AptSourceEntry] = {}
    for name, data in raw.items():
        if not isinstance(data, dict):
            raise ConfigError(f"apt_sources.{name} must be a table")
        ctx = f"apt_sources.{name}"
        source_file = str(_require_field(data, "source_file", ctx))
        if not _SAFE_FILENAME_RE.match(source_file):
            raise ConfigError(f"{ctx}.source_file must be a simple filename, got: {source_file}")
        entries[name] = AptSourceEntry(
            name=name,
            description=str(data.get("description", "")),
            key_url=str(_require_field(data, "key_url", ctx)),
            key_path=str(_require_field(data, "key_path", ctx)),
            source=str(_require_field(data, "source", ctx)),
            source_file=source_file,
            key_dearmor=bool(data.get("key_dearmor", False)),
            declared_at=decls.lookup("apt_sources", name),
        )
    return entries


def _load_apt_packages(
    raw: dict[str, object],
    decls: _SectionLineMap = _SYNTHESIZED_DECLS,
) -> dict[str, AptPackageEntry]:
    entries: dict[str, AptPackageEntry] = {}
    for name, data in raw.items():
        if not isinstance(data, dict):
            raise ConfigError(f"apt_packages.{name} must be a table")
        ctx = f"apt_packages.{name}"
        entries[name] = AptPackageEntry(
            name=name,
            description=str(data.get("description", "")),
            apt=_require_list(data, "apt", ctx),
            apt_sources=_require_list(data, "apt_sources", ctx) if "apt_sources" in data else [],
            declared_at=decls.lookup("apt_packages", name),
        )
    return entries


def publish_to(registry: Registry, config: Config | None = None) -> None:
    """Publish operator-declared TOML apt entries into the registry.

    Built-in apt entries no longer publish here: they ship as bundled YAML
    manifests under ``manifests/builtin/`` and land via
    ``builtin_manifests.publish_to`` (which runs first in
    ``build_registry``), with ``Origin.built_in`` and a shipped-file
    source, exactly like every other bundled resource. This function now
    carries only the operator's deprecated TOML surface for these two
    kinds (retired separately under ADR 0016).

    When ``config`` is provided, operator-declared entries
    (``[apt_sources.<name>]``, ``[apt_packages.<name>]`` in the operator's
    TOML) publish with ``Origin.operator_declared(...)``. Publish order +
    the kinds' ``builtin_override = "allow"`` policy is what lets the
    operator row replace the built-in at ``Registry.add``: the built-in
    manifests publish first, then this operator publisher, so an
    operator's override lands on top of the built-in base. Config-side
    publishing lives here (rather than in ``Config.publish_to``) because
    parsing operator apt entries is this module's expertise; Config just
    stashes the raw TOML dicts.

    ``declared_at`` falls through to the loaders' default synthesized shim
    here: the real section-line map is local to ``load_config`` and not
    carried on ``Config`` (which stashes only the raw section dicts), so
    the deprecated TOML surface keeps the ``line=0`` sentinel. Manifest
    entries carry a real location via the decoders.
    """
    if config is None:
        return

    from agentworks.config import CONFIG_PATH
    from agentworks.resources import Origin

    op_origin = Origin.operator_declared(file=CONFIG_PATH, line=0)
    for src_name, src in _load_apt_sources(config.apt_sources).items():
        registry.add("apt-source", src_name, src, op_origin)
    for pkg_name, pkg in _load_apt_packages(config.apt_packages).items():
        registry.add("apt-package", pkg_name, pkg, op_origin)


# -- Framework kind strategies -------------------------------------------------
#
# Both kinds use the **error miss policy**: a typo in an apt package's
# ``apt_sources`` list, or in a ``[vm_templates.*].apt_packages`` list,
# surfaces as a framework miss-policy error at ``build_registry`` time,
# citing the reference's source. There is no auto-declare path: entries are
# built-in (bundled manifests) or operator-declared, and references must
# resolve to a known name.
#
# ``apt-source`` was originally not a framework kind (only operator-facing
# config referenced by name got promoted at first). It joined the framework
# later so the ``apt-package -> apt-source`` dependency graph becomes visible
# on ``agw resource describe apt-source/<name>``'s ``Referenced by:`` section,
# and so unknown-source errors flow through the same miss-policy pipeline as
# everything else.


@dataclass(frozen=True)
class _AptSourceKind:
    """Implementation of ``ResourceKind`` for ``"apt-source"``."""

    kind: str = "apt-source"
    description: str = "3rd party apt repository definitions (key, source line)"
    miss_policy: Literal["auto-declare", "error"] = "error"
    auto_declare_names: frozenset[str] | None = None
    category: Literal["declarable", "capability"] = "declarable"
    builtin_override: Literal["allow", "reserved"] = "allow"

    def synthesize(self, references: Sequence[ResourceReference]) -> Any:
        return synthesize_no_default(self.kind, references)


@dataclass(frozen=True)
class _AptPackageKind:
    """Implementation of ``ResourceKind`` for ``"apt-package"``."""

    kind: str = "apt-package"
    description: str = "Named apt packages, optionally tied to apt-sources"
    miss_policy: Literal["auto-declare", "error"] = "error"
    auto_declare_names: frozenset[str] | None = None
    category: Literal["declarable", "capability"] = "declarable"
    builtin_override: Literal["allow", "reserved"] = "allow"

    def synthesize(self, references: Sequence[ResourceReference]) -> Any:
        return synthesize_no_default(self.kind, references)


KIND_REGISTRY["apt-source"] = _AptSourceKind()
KIND_REGISTRY["apt-package"] = _AptPackageKind()
