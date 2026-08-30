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
The optional app-shipped catalog is bundled with the ``apt`` system plugin;
operators may add or override entries via YAML manifests. Manifest decoders
delegate to ``_load_apt_sources`` and ``_load_apt_packages``.

``agentworks.resources.kinds.__init__`` imports this module so the two
kinds self-register into ``KIND_REGISTRY`` at load.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any, Literal, Self

from pydantic import Field, field_validator, model_validator

from agentworks.debian import DEBIAN_RELEASES, DebianRelease
from agentworks.declared_resource import DeclaredResource
from agentworks.errors import ConfigError
from agentworks.resource_loading import _require_field, _require_list
from agentworks.resources.kind import KIND_REGISTRY, synthesize_no_default
from agentworks.schema import ResourceRef
from agentworks.topics import TopicProse

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agentworks.resources.graph import FinalizeContext
    from agentworks.resources.reference import ResourceReference


# The ``source_file`` rule, authored once: a simple filename, no directory
# separators and no shell metacharacters, because it is interpolated into a
# shell command on the VM. Spelled as a pattern constraint rather than a
# validator so it reaches emitted JSON Schema and the explain surface as a
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
    ``origin`` as ``system-plugin`` for app-shipped optional entries or
    ``operator-declared`` for operator entries; ``references`` is attached by
    the framework's finalize pass from the apt_packages that name it).
    """

    # The examples on these four are what a generated sample writes on the
    # lines an operator MUST fill in: no default and no placeholder could
    # be right for a URL, a path, or a stanza, and `source_file`'s pattern
    # means a generic stand-in would not even load.
    key_url: str = Field(examples=["https://apt.example.com/key.gpg"])
    """Where to fetch the repository's signing key from."""

    key_path: str = Field(examples=["/etc/apt/keyrings/my-repo.gpg"])
    """Absolute path the fetched key is installed to on the VM."""

    source: str | None = Field(
        default=None,
        examples=[
            "deb [arch={arch} signed-by=/etc/apt/keyrings/my-repo.gpg] https://apt.example.com/debian bookworm main"
        ],
    )
    """The apt source-list stanza, verbatim (``deb [signed-by=...] ...``).

    ``{arch}`` stands for the VM's architecture (``amd64`` or ``arm64``).
    Use this form only when the repository value is release-independent."""

    sources: dict[DebianRelease, str] | None = Field(
        default=None,
        examples=[
            {
                "bookworm": "deb [arch={arch}] https://apt.example.com/debian bookworm main",
                "trixie": "deb [arch={arch}] https://apt.example.com/debian trixie main",
            }
        ],
    )
    """Release-specific source-list stanzas keyed by Debian codename."""

    source_file: SimpleFilename = Field(examples=["my-repo.list"])
    """Name of the file under ``/etc/apt/sources.list.d/`` the stanza is
    written to. A simple filename: no directory separators and no shell
    metacharacters, because it is interpolated into a shell command."""

    key_dearmor: bool = False
    """Whether the fetched key is ASCII-armored and must be run through
    ``gpg --dearmor`` before installation. Write booleans unquoted;
    quoted strings such as ``"no"`` are invalid."""

    @field_validator("sources", mode="before")
    @classmethod
    def _parse_release_keys(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        try:
            return {DebianRelease(key) if isinstance(key, str) else key: source for key, source in value.items()}
        except ValueError as exc:
            raise ValueError("sources keys must be recognized Debian codenames") from exc

    @model_validator(mode="after")
    def _validate_source_shape(self) -> Self:
        if (self.source is None) == (self.sources is None):
            raise ValueError("exactly one of source or sources is required")
        if self.source is not None:
            tokens = set(re.findall(r"[A-Za-z0-9_-]+", self.source.casefold()))
            codenames = {profile.release.value for profile in DEBIAN_RELEASES}
            if tokens & codenames:
                raise ValueError("a Debian-codename source must use the release-mapped sources field")
        return self

    def source_for(self, release: DebianRelease) -> str:
        """Resolve the source stanza for a verified VM release."""

        if self.source is not None:
            return self.source
        assert self.sources is not None
        try:
            return self.sources[release]
        except KeyError:
            raise ConfigError(
                f"apt source '{self.name}' has no Debian {release} mapping",
                entity_kind="apt-source",
                entity_name=self.name,
                hint=f"Add spec.sources.{release} to the apt-source manifest before retrying.",
            ) from None


class AptPackageEntry(DeclaredResource):
    """A named group of apt packages, optionally requiring one or more
    ``apt-source`` entries to be installed first.

    First-class, system-declared Registry citizen; the uniform metadata
    (name, description, origin, ...) comes from ``DeclaredResource``.
    """

    apt: list[str] = Field(default_factory=list)
    """The apt package names to install. May be empty when the entry exists
    only to install its ``apt_sources``."""

    apt_sources: list[Annotated[str, ResourceRef(kind="apt-source", usage="an apt source")]] = Field(
        default_factory=list
    )
    """Names of ``apt-source`` entries whose key and stanza must be
    installed before these packages can be."""

    def dependencies(self, context: FinalizeContext) -> list[ResourceReference]:
        """Emit one ``ResourceReference`` per name in ``apt_sources``. The
        framework's ``apt-source`` kind uses an ``error`` miss policy, so
        an unknown source name surfaces as a clean ``ConfigError`` at
        ``build_registry`` time with the referencing package's identity.

        The registry attaches the corresponding ``ReferenceEntry`` to
        each ``AptSourceEntry`` during finalize, so
        ``agw graph show apt-source/github`` shows every apt-package
        that depends on it.
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
            source=str(data["source"]) if "source" in data else None,
            sources=data.get("sources"),
            source_file=source_file,
            key_dearmor=bool(data.get("key_dearmor", False)),
        )
    return entries


def _load_apt_packages(
    raw: dict[str, object],
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
        )
    return entries


# The operator apt publisher was deleted with the TOML resource surface
# (ADR 0022): app-shipped apt entries are bundled YAML manifests in the
# optional ``apt`` system plugin, and operator apt entries are YAML manifests
# too. ``_load_apt_sources`` / ``_load_apt_packages`` above survive because
# the manifest apt decoders still delegate to them.


# -- Framework kind strategies -------------------------------------------------
#
# Both kinds use the **error miss policy**: a typo in an apt package's
# ``apt_sources`` list, or in a ``[vm_templates.*].apt_packages`` list,
# surfaces as a framework miss-policy error at ``build_registry`` time,
# citing the reference's source. There is no auto-declare path: entries are
# app-shipped through an opt-in system plugin or operator-declared, and
# references must resolve to a known name.
#
# ``apt-source`` was originally not a framework kind (only operator-facing
# config referenced by name got promoted at first). It joined the framework
# later so the ``apt-package -> apt-source`` dependency graph becomes visible
# as declared edges in ``agw graph show apt-source/<name>``,
# and so unknown-source errors flow through the same miss-policy pipeline as
# everything else.


@dataclass(frozen=True)
class _AptSourceKind:
    """Implementation of ``ResourceKind`` for ``"apt-source"``."""

    kind: str = "apt-source"
    description: str = "A third-party apt repository, with its signing key"
    prose: TopicProse = TopicProse(
        title="Apt sources",
        overview="""
        An apt-source is a third-party apt repository: where to fetch its signing key,
        where to install that key, and the source-list stanza that points at it. VM init
        writes the key and the stanza before installing anything that needs them.

        Declare exactly one source value shape. Use `source` for a repository whose stanza is
        independent of the guest's Debian release. Use `sources` when the value varies, keyed by
        every supported codename the source serves. A scalar containing a recognized Debian
        codename is rejected so a future release cannot accidentally reuse an old suite.

        Apt packages reference a source by name; nothing installs a source on its own.
        In the stanza, `{arch}` stands for the VM's architecture (`amd64` or `arm64`).
        Several sources ship through the optional `apt` plugin; declaring one under a plugin
        source name replaces it. A package retaining a shipped source dependency still needs that
        plugin enabled.
        """,
    )
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
    prose: TopicProse = TopicProse(
        title="Apt packages",
        overview="""
        An apt-package is a named SET of apt packages, plus the apt-sources they need.
        A vm-template refers to it by name through `apt_packages`, and VM init installs
        the sources first and the packages after.

        The indirection is what lets a template say `gh` without also knowing which
        repository provides it. Several packages ship through the optional `apt` plugin; declaring
        one under a plugin catalog name replaces it. A package retaining a shipped source dependency
        still needs the `apt` plugin enabled.
        """,
    )
    model: type[DeclaredResource] = AptPackageEntry
    miss_policy: Literal["auto-declare", "error"] = "error"
    auto_declare_names: frozenset[str] | None = None
    category: Literal["declarable", "capability"] = "declarable"
    builtin_override: Literal["allow", "reserved"] = "allow"

    def synthesize(self, references: Sequence[ResourceReference]) -> Any:
        return synthesize_no_default(self.kind, references)


KIND_REGISTRY["apt-source"] = _AptSourceKind()
KIND_REGISTRY["apt-package"] = _AptPackageKind()
