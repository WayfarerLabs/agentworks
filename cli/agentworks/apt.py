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
operators may add or override entries via YAML manifests. The
``_load_apt_sources`` / ``_load_apt_packages`` helpers below survive the
TOML sunset (ADR 0022) because the manifest apt decoders delegate to them.

``agentworks.resources.kinds.__init__`` imports this module so the two
kinds self-register into ``KIND_REGISTRY`` at load.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import Field

from agentworks.declared_resource import DeclaredResource
from agentworks.errors import ConfigError
from agentworks.resource_loading import (
    _SYNTHESIZED_DECLS,
    _require_field,
    _require_list,
)
from agentworks.resources.kind import KIND_REGISTRY, synthesize_no_default
from agentworks.schema import ResourceRef

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agentworks.config import _SectionLineMap
    from agentworks.resources.graph import FinalizeContext
    from agentworks.resources.reference import ResourceReference


# The ``source_file`` rule, authored once: a simple filename, no directory
# separators and no shell metacharacters, because it is interpolated into a
# shell command on the VM. Spelled as a pattern constraint rather than a
# validator so it reaches emitted JSON Schema and the describe surface as a
# fact rather than as behavior nobody outside this module can see.
_SAFE_FILENAME_PATTERN = r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$"
SimpleFilename = Annotated[str, Field(pattern=_SAFE_FILENAME_PATTERN)]

_SAFE_FILENAME_RE = re.compile(_SAFE_FILENAME_PATTERN)


# -- Rows --------------------------------------------------------------


class AptSourceEntry(DeclaredResource):
    """One apt repository source. Referenced by ``AptPackageEntry.apt_sources``
    when a package requires a source's key + list stanza before it can be
    installed. A first-class, system-declared Registry Resource: inherits the
    uniform metadata from ``DeclaredResource`` (the publisher stamps
    ``origin`` as ``built-in`` for shipped entries or ``operator-declared`` for
    config-added ones; ``references`` is attached by the framework's finalize
    pass from the apt_packages that name it).
    """

    key_url: str
    """Where to fetch the repository's signing key from."""

    key_path: str
    """Absolute path the fetched key is installed to on the VM."""

    source: str
    """The apt source-list stanza, verbatim (``deb [signed-by=...] ...``)."""

    source_file: SimpleFilename
    """Name of the file under ``/etc/apt/sources.list.d/`` the stanza is
    written to. A simple filename: no directory separators and no shell
    metacharacters, because it is interpolated into a shell command."""

    key_dearmor: bool = False
    """Whether the fetched key is ASCII-armored and must be run through
    ``gpg --dearmor`` before installation."""


class AptPackageEntry(DeclaredResource):
    """A named group of apt packages, optionally requiring one or more
    ``apt-source`` entries to be installed first.

    First-class, system-declared Registry citizen; the uniform metadata
    (name, description, origin, ...) comes from ``DeclaredResource``.
    """

    apt: list[str]
    """The apt package names to install."""

    apt_sources: list[Annotated[str, ResourceRef(kind="apt-source", usage="an apt source")]] = Field(
        default_factory=list
    )
    """Names of ``apt-source`` entries whose key and stanza must be
    installed before these packages can be."""

    def dependencies(self, context: FinalizeContext) -> list[ResourceReference]:
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
            description=str(data["description"]) if "description" in data else None,
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
            description=str(data["description"]) if "description" in data else None,
            apt=_require_list(data, "apt", ctx),
            apt_sources=_require_list(data, "apt_sources", ctx) if "apt_sources" in data else [],
            declared_at=decls.lookup("apt_packages", name),
        )
    return entries


# The operator apt publisher was deleted with the TOML resource surface
# (ADR 0022): built-in apt entries ship as bundled YAML manifests
# (``manifests/builtin/*.yaml``, via ``builtin_manifests.publish_to``), and
# operator apt entries are YAML manifests too. ``_load_apt_sources`` /
# ``_load_apt_packages`` above survive because the manifest apt decoders
# still delegate to them.


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
    model: type[DeclaredResource] = AptSourceEntry
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
    model: type[DeclaredResource] = AptPackageEntry
    miss_policy: Literal["auto-declare", "error"] = "error"
    auto_declare_names: frozenset[str] | None = None
    category: Literal["declarable", "capability"] = "declarable"
    builtin_override: Literal["allow", "reserved"] = "allow"

    def synthesize(self, references: Sequence[ResourceReference]) -> Any:
        return synthesize_no_default(self.kind, references)


KIND_REGISTRY["apt-source"] = _AptSourceKind()
KIND_REGISTRY["apt-package"] = _AptPackageKind()
