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
from agentworks.secrets import SecretConfig, SecretDecl
from agentworks.source_location import SourceLocation

if TYPE_CHECKING:
    from agentworks.agents.template import AgentTemplate
    from agentworks.git_credentials.credential import GitCredentialConfig
    from agentworks.resources.origin import Origin
    from agentworks.resources.registry import Registry
    from agentworks.sessions.template import NamedConsoleConfig, SessionTemplate
    from agentworks.vms.admin import AdminConfig
    from agentworks.vms.sites import VMSiteDecl
    from agentworks.vms.template import VMTemplate
    from agentworks.workspaces.template import WorkspaceTemplate

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


#: Backward-compatible alias; prefer ``OperatorConfig``.
UserConfig = OperatorConfig


@dataclass(frozen=True)
class PathsConfig:
    vm_workspaces: str = "/opt/agentworks/workspaces"
    vscode_workspaces: Path = field(default_factory=lambda: Path.home() / "aw-vscode-workspaces")
    backups: Path = field(default_factory=lambda: CONFIG_DIR / "backups")


@dataclass(frozen=True)
class DefaultsConfig:
    # Default vm-site name for `agw vm create` (validated against the
    # finalized registry by vms.validate_sites at the composition
    # boundary). The retired `defaults.platform` key is accepted as a
    # one-release deprecated alias.
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
    # None = the operator's TOML has no [named_console] section; the
    # framework's always-materialize pre-step auto-declares the default.
    named_console: NamedConsoleConfig | None
    vm_templates: dict[str, VMTemplate]
    # The file this Config was loaded from. The resources directory
    # (YAML manifests) is resolved relative to it, so tests loading
    # from tmp paths never pick up the developer's real manifests.
    source_path: Path
    # None = the operator's TOML has no [admin.*] sections (see
    # named_console above).
    admin: AdminConfig | None
    agent_templates: dict[str, AgentTemplate]
    session: SessionConfig
    session_templates: dict[str, SessionTemplate]
    workspace_templates: dict[str, WorkspaceTemplate]
    git_credentials: dict[str, GitCredentialConfig]
    apt_sources: dict[str, object] = field(default_factory=dict)
    apt_packages: dict[str, object] = field(default_factory=dict)
    system_install_commands: dict[str, object] = field(default_factory=dict)
    user_install_commands: dict[str, object] = field(default_factory=dict)
    # Legacy [azure] / [proxmox] TOML declarations of vm-site resources
    # (dual-path; deprecated). Keyed by site name (the section name).
    vm_sites: dict[str, VMSiteDecl] = field(default_factory=dict)
    # Env-and-secrets ----------------------------------------------------
    # Declared secrets, keyed by name. Empty when [secrets.*] is absent.
    secrets: dict[str, SecretDecl] = field(default_factory=dict)
    # Per-backend connection config keyed by kind ("env-var", "onepassword", ...).

    # Top-level [secret_config] table; carries the enabled-backends precedence list.
    secret_config_data: SecretConfig = field(default_factory=SecretConfig)
    config_issues: tuple[str, ...] = ()
    # Deprecation nudges (TOML resource sections, [secret_backends.*]
    # no-ops): a separate channel so real issues stay sharp for tests
    # and callers, and so --no-deprecations can silence only these.
    # ``deprecation_issues`` holds the ambient teaching messages;
    # ``deprecated_sections`` / ``noop_secret_backend_sections`` hold
    # the bare facts (display shapes of the sections present) for
    # surfaces that render their own tidy lines (doctor).
    deprecation_issues: tuple[str, ...] = ()
    deprecated_sections: tuple[str, ...] = ()
    noop_secret_backend_sections: tuple[str, ...] = ()
    # False when loaded with ``load_config(resources=False)`` (settings-only
    # callers); ``build_registry`` refuses such a Config so the TOML side
    # can never silently publish as empty.
    resources_loaded: bool = True

    def publish_to(self, registry: Registry) -> None:
        """Publish every operator-declared Resource into ``registry``.

        Iterates Config's per-kind dicts and pushes each Resource with an
        ``Origin.operator_declared(file=..., line=...)`` built from its
        ``declared_at: SourceLocation``. ``Config.admin`` and
        ``Config.named_console`` are operator-surface singletons today
        (one TOML block, one row published as ``admin-template:default``
        / ``named-console-template:default``), but their kinds are
        named-multi-instance in the framework. They publish only when
        the operator actually declared the sections (``None`` = absent);
        otherwise the framework's always-materialize pre-step
        auto-declares the default, exactly like vm-template and
        agent-template.

        ``secret_config`` is pure config and is NOT published: the
        chain is a setting that names resources, consumed by the
        secrets subsystem directly (validated against the finalized
        registry by ``secrets.validate_chain`` in ``build_registry``,
        read again at resolve time). Settings don't become
        pseudo-resources just because they point at resources.

        Imports ``Registry`` and ``Origin`` from ``agentworks.resources``
        -- the explicit layer handoff. Config's data structures (parsed
        Resources, ``SourceLocation``, etc.) remain framework-ignorant
        otherwise; only this publish handoff crosses the boundary.
        """
        # Import locally to keep ``agentworks.config`` import-light at
        # module load: bootstrap code paths that don't touch Resources
        # (e.g., `from agentworks.config import CONFIG_PATH`) avoid
        # pulling in the full framework.
        from agentworks.resources import Origin

        def op_origin(declared_at: SourceLocation) -> Origin:
            return Origin.operator_declared(file=declared_at.file, line=declared_at.line)

        # Multi-named kinds: one Resource per (container, name) pair.
        # secret_backends is deliberately absent: the TOML sections are
        # deprecated no-ops (built-in backends ship as bundled
        # manifests; new backends are manifest-declared).
        for kind, kind_dict in (
            ("secret", self.secrets),
            ("vm-template", self.vm_templates),
            ("agent-template", self.agent_templates),
            ("workspace-template", self.workspace_templates),
            ("session-template", self.session_templates),
            ("git-credential", self.git_credentials),
            ("vm-site", self.vm_sites),
        ):
            for name, resource in kind_dict.items():
                registry.add(kind, name, resource, op_origin(resource.declared_at))

        # Operator-surface-singleton kinds publish only when declared;
        # an absent section means the framework auto-declares the
        # default at finalize (no synthesized placeholder rows, no
        # collision exemption).
        if self.admin is not None:
            registry.add(
                "admin-template",
                self.admin.name,
                self.admin,
                op_origin(self.admin.declared_at),
            )
        if self.named_console is not None:
            registry.add(
                "named-console-template",
                "default",
                self.named_console,
                op_origin(self.named_console.declared_at),
            )


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
