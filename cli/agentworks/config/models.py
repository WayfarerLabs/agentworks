"""Config's settings dataclasses, the ``Config`` object itself, and the
section-line-map helper used to attach ``declared_at`` locations to loaded
resources.

Split out of the former monolithic ``agentworks/config.py`` (see
``agentworks/config/__init__.py`` for the package overview).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from agentworks.config.validation import CONFIG_DIR
from agentworks.secrets import SecretConfig
from agentworks.source_location import SourceLocation

if TYPE_CHECKING:
    from agentworks.resources.registry import Registry

# -- Data classes ----------------------------------------------------------


@dataclass(frozen=True)
class OperatorConfig:
    ssh_public_key: Path
    ssh_private_key: Path
    ssh_config: Path = field(default_factory=lambda: Path.home() / ".ssh" / "config")
    ssh_config_dir: bool = True
    ssh_host_prefix: str = "awvm--"
    ssh_agent_host_prefix: str = "awagent--"
    extra_ssh_public_keys: list[Path] = field(default_factory=list)
    # Extra source addresses (normalized IPv4 CIDRs; a bare IP loads as
    # its /32) allowed through the transient cloud SSH firewall hole,
    # alongside the auto-detected operator egress IP. For operators
    # whose SSH traffic egresses somewhere detection cannot see (VPN
    # split tunnels, proxies, CGNAT).
    ssh_allow_cidrs: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class PathsConfig:
    vm_workspaces: str = "/opt/agentworks/workspaces"
    vscode_workspaces: Path = field(default_factory=lambda: Path.home() / "aw-vscode-workspaces")
    backups: Path = field(default_factory=lambda: CONFIG_DIR / "backups")


@dataclass(frozen=True)
class DefaultsConfig:
    # Default vm-site name for `agw vm create` (validated against the
    # finalized registry by vms.validate_sites at the composition
    # boundary).
    site: str | None = None
    # Run the git-credential runup stage: authenticate each token against
    # its provider API before it is written. Definitive rejection (401)
    # is handled by the provisioning logic; network indeterminacy only
    # warns. Off for airgapped setups.
    runup_git_credentials: bool = True


@dataclass(frozen=True)
class SessionConfig:
    history_limit: int = 50_000


@dataclass(frozen=True)
class Config:
    operator: OperatorConfig
    paths: PathsConfig
    defaults: DefaultsConfig
    # The file this Config was loaded from. The resources directory
    # (YAML manifests) is resolved relative to it, so tests loading
    # from tmp paths never pick up the developer's real manifests.
    source_path: Path
    session: SessionConfig
    # config.toml is settings only now (ADR 0022): every resource is a YAML
    # manifest, so Config carries no resource dicts. Resources are read from
    # the registry (built from the bundled + operator manifests), never from
    # Config.

    # Top-level [secret_config] table; carries the enabled-backends precedence list.
    secret_config_data: SecretConfig = field(default_factory=SecretConfig)
    # The [plugins] table's ``system`` key; the opt-in list of enabled
    # system plugin names (R4). Named ``enabled_system_plugins`` (not a
    # mechanical ``plugins_system``) to read clearly and to stay distinct
    # from ``SYSTEM_PLUGINS``, the index of ALL installed system plugins;
    # this field is the operator-enabled subset. A setting, not a resource,
    # carried exactly like secret_config_data above: empty when [plugins]
    # is absent, present on both load paths, and never published as a
    # pseudo-resource (see the secret_config non-publication note in
    # publish_to below; the same rationale applies here). Consumed by
    # plugins.publish_plugins / build_registry.
    enabled_system_plugins: tuple[str, ...] = ()
    config_issues: tuple[str, ...] = ()
    # Deprecation nudges: a separate channel from ``config_issues`` so real
    # issues stay sharp for tests and callers, and so --no-deprecations can
    # silence only these.
    #
    # EMPTY TODAY, and honestly so: both nudges that ever rode it are hard
    # errors now (the TOML resource sections, then the ``[secret_backends.*]``
    # no-op that was the last producer), so nothing populates it. It is kept
    # as the mechanism, not as a half-migration: it is generic, it is backed
    # by an operator-facing CLI flag, and the next deprecation wants it.
    # KEPT DELIBERATELY, not pending retirement (operator ruling,
    # 2026-08-07): being empty is not the test, because a warn window is
    # exactly the thing you cannot build at the moment you need it. The
    # split is per-SOURCE carrier into a shared surface, so a deprecation
    # from somewhere other than settings adds its own carrier and reuses
    # ``--no-deprecations`` and ``output.deprecations_suppressed`` rather
    # than widening this field to mean something it does not.
    deprecation_issues: tuple[str, ...] = ()

    def publish_to(self, registry: Registry) -> None:
        """Publish Config's resources into ``registry`` (now a no-op).

        config.toml is settings only (ADR 0022): every resource is a YAML
        manifest published by ``ManifestSet.publish_to``, so Config has no
        resources to publish. The method is kept as the (now-empty) Config
        arm of the publisher protocol ``bootstrap.build_registry`` drives.

        ``secret_config`` and ``enabled_system_plugins`` are settings, not
        resources: they are consumed directly (``secrets.validate_chain`` /
        ``plugins.publish_plugins`` in ``build_registry``), never published
        as pseudo-resources.
        """
        return


# -- Loading ---------------------------------------------------------------


@dataclass(frozen=True)
class _SectionLineMap:
    """Resolves ``declared_at`` for a Resource from the pre-scanned section
    -line map. Bundles the config file path with the dotted-section-path ->
    line map so loaders can call ``decls.lookup("vm_templates", name)``
    and get a fully-populated ``SourceLocation`` back.
    """

    config_path: Path
    section_lines: dict[tuple[str, ...], int]

    def lookup(self, *path: str) -> SourceLocation:
        """Return ``SourceLocation`` for the Resource at the given section
        path. Picks the earliest contributing header (the section itself or
        any sub-section under it). If nothing matches
        (the Resource is synthesized by code rather than declared by the
        operator), returns ``SourceLocation(config_path, line=0)``.
        """
        n = len(path)
        candidates = [line for p, line in self.section_lines.items() if len(p) >= n and p[:n] == path]
        if not candidates:
            return SourceLocation(file=self.config_path, line=0)
        return SourceLocation(file=self.config_path, line=min(candidates))
