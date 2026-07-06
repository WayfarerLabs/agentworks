"""Agentworks configuration loading and validation.

Config lives at ~/.config/agentworks/config.toml. It is read-only at runtime.
"""

from __future__ import annotations

import re
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

# ConfigError is defined in agentworks.errors and re-exported here for backward
# compatibility with existing `from agentworks.config import ConfigError` users.
# The `X as X` shape marks the name as an explicit re-export for mypy strict mode.
from agentworks.env import EnvEntry
from agentworks.errors import ConfigError as ConfigError
from agentworks.secrets import (
    SecretConfig,
    SecretDecl,
)
from agentworks.source_location import SourceLocation, scan_section_lines, synthesized

if TYPE_CHECKING:

    from agentworks.resources.origin import Origin
    from agentworks.resources.reference import (
        ReferenceEntry,
        ResourceReference,
        SecretReference,
    )
    from agentworks.resources.registry import Registry

CONFIG_DIR = Path.home() / ".config" / "agentworks"
CONFIG_PATH = CONFIG_DIR / "config.toml"

NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9_-]*[a-z0-9])?$")
# Linux username: alphanumeric, hyphens, underscores; 1-32 chars
VM_USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
# SSH host prefix: alphanumeric, hyphens, underscores, dots
SSH_HOST_PREFIX_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")

MAX_NAME_LENGTH = 30


def validate_name(name: str, *, allow_double_hyphen: bool = False) -> None:
    """Validate a resource name, raising ValidationError on failure.

    Rules: lowercase alphanumeric, hyphens, underscores. Must start and end
    with alphanumeric. Max 30 characters (leaves room for agent username
    derivation within the 32-character Linux username limit).

    Consecutive hyphens (``--``) are rejected by default because they are
    reserved for the ``<workspace>--<agent>`` separator used by the legacy
    agent-derivation scheme; new resource names need headroom for that.
    Pass ``allow_double_hyphen=True`` only when validating a name that is
    being used to *look up* an existing entity (the DB is the ultimate
    arbiter of existence; the validator only sanitizes characters). Legacy
    sessions predating the rule use ``--`` in their names and still need to
    be deletable / attachable / addable to consoles.
    """
    from agentworks.output import ValidationError

    if len(name) > MAX_NAME_LENGTH:
        raise ValidationError(
            f"name '{name}' is too long ({len(name)} chars, max {MAX_NAME_LENGTH})"
        )
    if not NAME_RE.match(name) or (not allow_double_hyphen and "--" in name):
        suffix = (
            ""
            if allow_double_hyphen
            else ", and cannot contain consecutive hyphens (--)"
        )
        raise ValidationError(
            f"invalid name '{name}'. Names must be lowercase alphanumeric "
            "with hyphens or underscores, must start and end with a letter or "
            f"digit{suffix}."
        )


def validate_admin_username(admin_username: str) -> None:
    """Validate an admin username for shell and OS safety."""
    from agentworks.output import ValidationError

    if not VM_USER_RE.match(admin_username):
        raise ValidationError(
            f"invalid admin_username '{admin_username}'. Must be a valid Linux username "
            "(lowercase, alphanumeric/hyphens/underscores, max 32 chars)"
        )


# Valid values for enum-like fields. Git credential ``type`` validation
# moved to the framework's ``git-credential-provider`` kind in Phase 2b.1
# (see ``agentworks.git_credentials.PROVIDER_TYPES`` for the canonical
# list).
VALID_PLATFORMS = ("lima", "azure", "wsl2", "proxmox")


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
    platform: str | None = None
    vm_host: str | None = None


# Agentworks-specific layout: session pane (pane 0) takes the top 50% of
# the window, shell panes stack vertically in the bottom 50% with equal
# heights. tmux has no preset that matches this geometry, so apply-time
# builds a custom tmux layout string from the live window dimensions and
# pane IDs and feeds it to `tmux select-layout`. See
# `_apply_aw_session_vertical_layout` in sessions/multi_console_layout.py.
AW_SESSION_VERTICAL_LAYOUT = "aw-session-vertical"

# Valid layouts for named-console session windows. All values besides
# AW_SESSION_VERTICAL_LAYOUT map 1:1 to tmux's built-in select-layout
# names so operators can apply the same value to a window via
# `tmux select-layout` on the fly.
VALID_TMUX_LAYOUTS = (
    "tiled",
    "even-vertical",
    "even-horizontal",
    "main-vertical",
    "main-horizontal",
    AW_SESSION_VERTICAL_LAYOUT,
)


def _env_references(
    env: dict[str, EnvEntry] | None,
    source: tuple[str, str],
) -> list[SecretReference]:
    """Aggregate ``EnvEntry.referenced_resources(source)`` across an env table.

    Module-level helper shared by every env-bearing Resource type's
    ``referenced_resources()`` method so the per-type method body stays
    one line. ``env`` may be ``None`` (``SessionTemplate.env`` is
    optional) or empty, in which case the result is an empty list.
    """
    if not env:
        return []
    out: list[SecretReference] = []
    for entry in env.values():
        out.extend(entry.referenced_resources(source))
    return out


def _git_credential_references(
    git_credentials: list[str] | None,
    source: tuple[str, str],
) -> list[ResourceReference]:
    """Emit a ``ResourceReference`` of kind ``"git-credential"`` per
    name in ``git_credentials``. Used by ``AdminConfig.referenced_resources``
    and ``AgentTemplate.referenced_resources`` to feed the
    ``GitCredentialKind``'s error miss policy: a typo'd or undeclared
    name errors at finalize with the reference source pointing at the
    declaring Resource.
    """
    from agentworks.resources.reference import ResourceReference

    if not git_credentials:
        return []
    return [
        ResourceReference(
            name=cred_name,
            kind="git-credential",
            usage="the git credential",
            source=source,
        )
        for cred_name in git_credentials
    ]


def _tailscale_secret_reference(
    tailscale_auth_key: str,
    template_name: str,
) -> SecretReference:
    """Build the ``SecretReference`` a VMTemplate publishes for its
    Tailscale auth key. Used by both ``VMTemplate.referenced_resources``
    (raw, in this module) and ``ResolvedVMTemplate.referenced_resources``
    (resolved, in ``agentworks.vms.templates``) so the reference shape
    is single-sourced.
    """
    from agentworks.resources.reference import SecretReference

    return SecretReference(
        name=tailscale_auth_key,
        kind="secret",
        usage="the Tailscale auth key",
        source=("vm-template", template_name),
    )


@dataclass(frozen=True)
class NamedConsoleConfig:
    """Settings for the `console` subcommand group (named multi-session
    consoles). Section is `[named_console]` in the TOML to disambiguate from
    the legacy `vm console` and the workspace console template. Only named
    consoles read these values today.
    """

    tmux_layout: str = AW_SESSION_VERTICAL_LAYOUT
    declared_at: SourceLocation = field(default_factory=synthesized)
    origin: Origin | None = None
    references: tuple[ReferenceEntry, ...] = ()


@dataclass(frozen=True)
class VMTemplate:
    """VM template definition. All optional fields use ``None = inherit``
    semantics except ``tailscale_auth_key``, which is a non-optional
    bare-string secret name (default ``"tailscale-auth-key"``). The
    tailscale field carries no inherit shape because the secret name is a
    deployment-wide convention; operators who want a different name per
    template set it on the specific template.
    """

    name: str
    inherits: list[str] = field(default_factory=list)
    # Provisioning
    cpus: int | None = None
    memory: int | None = None
    disk: int | None = None
    azure_vm_size: str | None = None
    swap: int | None = None
    # System-wide initialization
    apt: list[str] | None = None
    apt_packages: list[str] | None = None
    snap: list[str] | None = None
    system_install_commands: list[str] | None = None
    # Env (declared per-template; merged child-overrides-parent at resolution).
    # Plaintext or secret references; the loader produces EnvEntry instances.
    env: dict[str, EnvEntry] = field(default_factory=dict)
    # Secret name for the Tailscale auth key. ``None = inherit`` per the
    # convention used by VMTemplate's other optional fields; the loader
    # sets it to the operator's string when explicit, to ``None`` when
    # omitted. ResolvedVMTemplate (in agentworks.vms.templates) carries
    # the post-inheritance resolved string (default ``"tailscale-auth-key"``).
    # Bare-string only -- no ``{ secret = "..." }`` polymorphism per the
    # SDD; the field IS the secret reference.
    tailscale_auth_key: str | None = None
    declared_at: SourceLocation = field(default_factory=synthesized)
    origin: Origin | None = None
    references: tuple[ReferenceEntry, ...] = ()

    def referenced_resources(self) -> list[ResourceReference]:
        from agentworks.resources.reference import (
            ResourceReference as _ResourceReq,
        )
        from agentworks.resources.reference import (
            TemplateReference,
        )

        source = ("vm-template", self.name)
        refs: list[ResourceReference] = list(_env_references(self.env, source))
        # Inherits: each parent template name in ``inherits = [...]`` is a
        # TemplateReference targeting the same kind. The framework's
        # VMTemplateKind miss policy auto-declares "default" when missing
        # and errors on any other unknown name; framework cycle detection
        # catches inheritance loops. Per-template field-merging stays in
        # ``agentworks.vms.templates``.
        for parent in self.inherits:
            refs.append(
                TemplateReference(
                    name=parent,
                    kind="vm-template",
                    usage="a parent template",
                    source=source,
                )
            )
        # Catalog references: each name in apt_packages /
        # system_install_commands resolves to a built-in catalog
        # Resource via the framework's miss policy (error on typo,
        # citing this template's source). Phase 2b.
        for pkg in self.apt_packages or []:
            refs.append(
                _ResourceReq(
                    name=pkg,
                    kind="apt-package",
                    usage="an apt package",
                    source=source,
                )
            )
        for cmd in self.system_install_commands or []:
            refs.append(
                _ResourceReq(
                    name=cmd,
                    kind="system-install-command",
                    usage="a system install command",
                    source=source,
                )
            )
        # When the raw template doesn't set tailscale_auth_key, emit the
        # default secret name's reference so the registry finalizes
        # cleanly even before any inheritance walk. ResolvedVMTemplate's
        # referenced_resources emits the inherited value at manager-entry
        # call time.
        ts_name = self.tailscale_auth_key or "tailscale-auth-key"
        refs.append(_tailscale_secret_reference(ts_name, self.name))
        return refs


@dataclass(frozen=True)
class AdminConfig:
    """Per-user config for the admin user on VMs.

    Phase 2a.3 plurified the underlying ``admin-template`` kind from
    singleton-conceptual to named-multi-instance: ``AdminConfig`` now
    carries its own ``name`` (default ``"default"``) just like the other
    template kinds. The operator-facing surface is unchanged in this
    phase -- the loader only accepts the ``[admin]`` block and produces
    one instance with name ``"default"``. A future SDD adds
    ``[admin_templates.<name>]`` parsing, the ``--admin-template`` CLI
    flag, and the VM DB column; that work can land without re-touching
    the framework.
    """

    name: str = "default"
    username: str = "agentworks"
    shell: str = "bash"
    git_credentials: list[str] = field(default_factory=list)
    user_install_commands: list[str] = field(default_factory=list)
    dotfiles_source: str | None = None
    dotfiles_destination: str = "~/.dotfiles"
    dotfiles_install_cmd: str = "./install.sh"
    mise_activate: bool = True
    mise_packages: list[str] = field(default_factory=list)
    mise_lockfile: str | None = None
    mise_allow_unlocked: bool = False
    mise_install_before: str = "7d"
    mise_prune_on_reinit: bool = True
    git_force_safe_directory: bool = True
    # Claude Code
    claude_marketplaces: list[str] = field(default_factory=list)
    claude_plugins: list[str] = field(default_factory=list)
    # Env that applies whenever a shell is opened as the admin user.
    env: dict[str, EnvEntry] = field(default_factory=dict)
    declared_at: SourceLocation = field(default_factory=synthesized)
    origin: Origin | None = None
    references: tuple[ReferenceEntry, ...] = ()

    def referenced_resources(self) -> list[ResourceReference]:
        from agentworks.resources.reference import (
            ResourceReference as _ResourceReq,
        )

        source = ("admin-template", self.name)
        refs: list[ResourceReference] = list(
            _env_references(self.env, source)
        )
        refs.extend(_git_credential_references(self.git_credentials, source))
        # Catalog references for user_install_commands (Phase 2b).
        for cmd in self.user_install_commands:
            refs.append(
                _ResourceReq(
                    name=cmd,
                    kind="user-install-command",
                    usage="a user install command",
                    source=source,
                )
            )
        return refs


@dataclass(frozen=True)
class AgentTemplate:
    """Agent template definition. All fields are optional (None = inherit/default)."""

    name: str
    inherits: list[str] = field(default_factory=list)
    shell: str | None = None
    git_credentials: list[str] | None = None
    user_install_commands: list[str] | None = None
    dotfiles_source: str | None = None
    dotfiles_destination: str | None = None
    dotfiles_install_cmd: str | None = None
    mise_activate: bool | None = None
    mise_packages: list[str] | None = None
    mise_lockfile: str | None = None
    mise_allow_unlocked: bool | None = None
    mise_install_before: str | None = None
    mise_prune_on_reinit: bool | None = None
    claude_marketplaces: list[str] | None = None
    claude_plugins: list[str] | None = None
    env: dict[str, EnvEntry] = field(default_factory=dict)
    declared_at: SourceLocation = field(default_factory=synthesized)
    origin: Origin | None = None
    references: tuple[ReferenceEntry, ...] = ()

    def referenced_resources(self) -> list[ResourceReference]:
        from agentworks.resources.reference import (
            ResourceReference as _ResourceReq,
        )
        from agentworks.resources.reference import (
            TemplateReference,
        )

        source = ("agent-template", self.name)
        refs: list[ResourceReference] = list(
            _env_references(self.env, source)
        )
        refs.extend(_git_credential_references(self.git_credentials, source))
        for parent in self.inherits:
            refs.append(
                TemplateReference(
                    name=parent,
                    kind="agent-template",
                    usage="a parent template",
                    source=source,
                )
            )
        # Catalog references for user_install_commands (Phase 2b).
        for cmd in self.user_install_commands or []:
            refs.append(
                _ResourceReq(
                    name=cmd,
                    kind="user-install-command",
                    usage="a user install command",
                    source=source,
                )
            )
        return refs


@dataclass(frozen=True)
class WorkspaceTemplate:
    name: str
    inherits: list[str] = field(default_factory=list)
    repo: str | None = None
    tmuxinator: bool | None = None  # None = not explicitly set (inherit/default to True)
    env: dict[str, EnvEntry] = field(default_factory=dict)
    declared_at: SourceLocation = field(default_factory=synthesized)
    origin: Origin | None = None
    references: tuple[ReferenceEntry, ...] = ()

    def referenced_resources(self) -> list[ResourceReference]:
        from agentworks.resources.reference import TemplateReference

        source = ("workspace-template", self.name)
        refs: list[ResourceReference] = list(_env_references(self.env, source))
        for parent in self.inherits:
            refs.append(
                TemplateReference(
                    name=parent,
                    kind="workspace-template",
                    usage="a parent template",
                    source=source,
                )
            )
        return refs


@dataclass(frozen=True)
class GitCredentialConfig:
    name: str
    # The internal representation follows the YAML manifest shape (ADR
    # 0016): field name ``provider``, matching ``spec.provider``. Only
    # the TOML section still spells ``type`` (with ``provider`` as the
    # preferred alias); the loader maps at its boundary.
    provider: str
    # Provider-owned configuration (azdo's org), nested per the
    # provider_config pattern (ADR 0016). The flat TOML section is the
    # ONLY place org lives at the top level; this loader nests it at
    # the boundary, so the internal representation matches the YAML
    # manifest shape.
    provider_config: dict[str, object] = field(default_factory=dict)
    description: str | None = None
    # Secret name for the auth token. Default ``"git-token-<name>"`` is
    # computed in ``__post_init__`` (the per-credential default depends
    # on the credential's own name, which a class-level literal can't
    # express). Operators may override with a custom secret name; the
    # framework's ``"secret"`` kind then resolves the value. Bare-string
    # only per Phase 1c's pattern; no ``{ secret = "..." }``
    # polymorphism.
    token: str = ""
    declared_at: SourceLocation = field(default_factory=synthesized)
    origin: Origin | None = None
    references: tuple[ReferenceEntry, ...] = ()

    def __post_init__(self) -> None:
        # Frozen dataclasses can still ``object.__setattr__`` during
        # construction. The default ``""`` sentinel triggers the
        # name-interpolated default; an operator-typed string survives
        # unchanged.
        if not self.token:
            object.__setattr__(self, "token", f"git-token-{self.name}")

    def referenced_resources(self) -> list[ResourceReference]:
        from agentworks.resources.reference import (
            ResourceReference as _ResourceReq,
        )
        from agentworks.resources.reference import SecretReference

        source = ("git-credential", self.name)
        return [
            SecretReference(
                name=self.token,
                kind="secret",
                usage="the auth token",
                source=source,
            ),
            # Phase 2b.1: the ``provider`` field references a known
            # provider kind; framework miss policy catches typos.
            _ResourceReq(
                name=self.provider,
                kind="git-credential-provider",
                usage="the provider",
                source=source,
            ),
        ]


@dataclass(frozen=True)
class SessionTemplate:
    """Session template definition. All fields optional (None = inherit/default)."""

    name: str
    inherits: list[str] = field(default_factory=list)
    command: str | None = None
    description: str | None = None
    restart_command: str | None = None
    required_commands: list[str] | None = None
    env: dict[str, EnvEntry] | None = None
    declared_at: SourceLocation = field(default_factory=synthesized)
    origin: Origin | None = None
    references: tuple[ReferenceEntry, ...] = ()

    def referenced_resources(self) -> list[ResourceReference]:
        from agentworks.resources.reference import TemplateReference

        source = ("session-template", self.name)
        refs: list[ResourceReference] = list(_env_references(self.env, source))
        for parent in self.inherits:
            refs.append(
                TemplateReference(
                    name=parent,
                    kind="session-template",
                    usage="a parent template",
                    source=source,
                )
            )
        return refs


@dataclass(frozen=True)
class SessionConfig:
    history_limit: int = 50_000


@dataclass(frozen=True)
class AzureConfig:
    subscription_id: str
    resource_group: str
    region: str
    idle_timeout_hours: int = 2


@dataclass(frozen=True)
class ProxmoxConfig:
    api_url: str
    node: str
    token_id: str
    template_vmid: int
    storage: str = "local-lvm"
    bridge: str = "vmbr0"
    pool: str = "agentworks"
    verify_ssl: bool = True


@dataclass(frozen=True)
class Config:
    operator: OperatorConfig
    paths: PathsConfig
    defaults: DefaultsConfig
    named_console: NamedConsoleConfig
    vm_templates: dict[str, VMTemplate]
    # The file this Config was loaded from. The resources directory
    # (YAML manifests) is resolved relative to it, so tests loading
    # from tmp paths never pick up the developer's real manifests.
    source_path: Path
    admin: AdminConfig
    agent_templates: dict[str, AgentTemplate]
    session: SessionConfig
    session_templates: dict[str, SessionTemplate]
    workspace_templates: dict[str, WorkspaceTemplate]
    git_credentials: dict[str, GitCredentialConfig]
    apt_sources: dict[str, object] = field(default_factory=dict)
    apt_packages: dict[str, object] = field(default_factory=dict)
    system_install_commands: dict[str, object] = field(default_factory=dict)
    user_install_commands: dict[str, object] = field(default_factory=dict)
    azure: AzureConfig | None = None
    proxmox: ProxmoxConfig | None = None
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
    deprecation_issues: tuple[str, ...] = ()

    def publish_to(self, registry: Registry) -> None:
        """Publish every operator-declared Resource into ``registry``.

        Iterates Config's per-kind dicts and pushes each Resource with an
        ``Origin.operator_declared(file=..., line=...)`` built from its
        ``declared_at: SourceLocation``. ``Config.admin`` and
        ``Config.named_console`` are operator-surface singletons today
        (one TOML block, one row published as ``admin-template:default``
        / ``named-console-template:default``), but their kinds are
        named-multi-instance in the framework: a future SDD can grow the
        operator surface to ``[admin_templates.<name>]`` /
        ``[named_console_templates.<name>]`` without re-touching the
        framework. Phase 0's loader always produces an instance even
        when the operator's TOML omits all sections; Config.publish_to
        always publishes it.

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
            return Origin.operator_declared(
                file=declared_at.file, line=declared_at.line
            )

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
        ):
            for name, resource in kind_dict.items():
                registry.add(kind, name, resource, op_origin(resource.declared_at))

        # Operator-surface-singleton kinds: framework treats them as
        # named-multi-instance per Phase 2a.3, but today's loader only
        # produces one row each (``admin-template:default`` /
        # ``named-console-template:default``). admin-template's name is
        # carried on the AdminConfig itself now -- still defaults to
        # "default" -- so the future plurified surface can land without
        # changing this publish line.
        registry.add(
            "admin-template",
            self.admin.name,
            self.admin,
            op_origin(self.admin.declared_at),
        )
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
        any sub-section under it) per Phase 0's design. If nothing matches
        (the Resource is synthesized by code rather than declared by the
        operator), returns ``SourceLocation(config_path, line=0)``.
        """
        n = len(path)
        candidates = [
            line
            for p, line in self.section_lines.items()
            if len(p) >= n and p[:n] == path
        ]
        if not candidates:
            return SourceLocation(file=self.config_path, line=0)
        return SourceLocation(file=self.config_path, line=min(candidates))


def _expand(path_str: str) -> Path:
    return Path(path_str).expanduser()


def _require(data: dict[str, object], key: str, context: str) -> object:
    if key not in data:
        raise ConfigError(f"{context}.{key} is required")
    return data[key]


def _require_string_list(data: dict[str, object], key: str, context: str) -> list[str]:
    """Load a key as a list of strings, raising ConfigError on type mismatch."""
    val = data.get(key, [])
    if not isinstance(val, list) or not all(isinstance(v, str) for v in val):
        raise ConfigError(f"{context}.{key} must be a list of strings")
    return val


def _warn_unexpected_keys(
    raw: dict[str, object],
    known: set[str],
    section: str,
    issues: list[str],
) -> None:
    """Record unexpected keys in a config section.

    This catches the common TOML pitfall where a [section] header is
    commented out and its keys land in the previous section, as well as
    typos and version mismatches. Issues are collected on the Config object
    so that doctor can report all of them without short-circuiting.
    """
    unexpected = set(raw.keys()) - known
    if unexpected:
        keys = ", ".join(sorted(unexpected))
        issues.append(f"unexpected keys in [{section}]: {keys}")


_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_AGENTWORKS_ENV_PREFIX = "AGENTWORKS_"


def _parse_env_table(
    raw_env: object,
    *,
    context: str,
    issues: list[str],
) -> dict[str, EnvEntry]:
    """Parse a TOML env table into ``dict[str, EnvEntry]``.

    Two value shapes per key:

    - bare string: ``KEY = "value"`` produces ``EnvEntry(key, value=...)``.
    - inline table with secret: ``KEY = { secret = "name" }`` produces
      ``EnvEntry(key, secret=...)``.

    Any other shape raises ``ConfigError``. ``AGENTWORKS_*`` keys append a
    load-time warning to ``issues`` (operators are discouraged from overriding
    agentworks-managed identity vars). Missing or None input returns ``{}``.
    """
    if raw_env is None:
        return {}
    if not isinstance(raw_env, dict):
        raise ConfigError(f"{context}.env must be a table")

    result: dict[str, EnvEntry] = {}
    for key, val in raw_env.items():
        key_str = str(key)
        if not _ENV_KEY_RE.match(key_str):
            raise ConfigError(
                f"{context}.env: invalid env var name {key_str!r} "
                "(must match /^[A-Za-z_][A-Za-z0-9_]*$/)"
            )
        if key_str.startswith(_AGENTWORKS_ENV_PREFIX):
            issues.append(
                f"{context}.env sets agentworks-managed identity variable "
                f"{key_str!r}; identity values win at the runtime prelude, "
                "so your value will be ignored at command time. Remove the entry."
            )
        if isinstance(val, str):
            # ADR 0014: newlines in env values would corrupt the SSH
            # `-o SetEnv=KEY=VALUE` argument shape. Warn at load time so
            # operators catch accidental trailing newlines (a common
            # copy-paste artifact). The resolve loop applies the
            # same check defensively to secret-resolved values.
            if "\n" in val or "\r" in val:
                issues.append(
                    f"{context}.env.{key_str}: value contains a newline; "
                    "SSH SetEnv cannot transport it cleanly. Strip the "
                    "newline at the source."
                )
            result[key_str] = EnvEntry(key=key_str, value=val)
        elif isinstance(val, dict):
            extra = set(val.keys()) - {"secret"}
            if extra:
                raise ConfigError(
                    f"{context}.env.{key_str}: unexpected keys {sorted(extra)}; "
                    "only 'secret' is supported in env-entry inline tables"
                )
            secret_name = val.get("secret")
            if not isinstance(secret_name, str):
                raise ConfigError(
                    f"{context}.env.{key_str}: inline table must set "
                    "'secret = \"<name>\"' (or use a bare string for plaintext)"
                )
            result[key_str] = EnvEntry(key=key_str, secret=secret_name)
        else:
            raise ConfigError(
                f"{context}.env.{key_str}: must be a string (plaintext) or "
                "inline table of the form { secret = \"<name>\" }"
            )
    return result


_OPERATOR_KEYS = {
    "ssh_public_key",
    "ssh_private_key",
    "ssh_config",
    "ssh_config_dir",
    "ssh_host_prefix",
    "ssh_agent_host_prefix",
    "extra_ssh_public_keys",
}


def _load_operator(data: dict[str, object], issues: list[str]) -> OperatorConfig:
    raw = data.get("operator")
    section_name = "operator"
    if not isinstance(raw, dict):
        # Accept [user] as a deprecated alias for [operator]
        raw = data.get("user")
        if isinstance(raw, dict):
            print(
                "WARNING: config [user] section is deprecated; rename it to [operator].",
                file=sys.stderr,
            )
            section_name = "user"
        else:
            raise ConfigError("[operator] section is required")

    _warn_unexpected_keys(raw, _OPERATOR_KEYS, section_name, issues)

    pub = _expand(str(_require(raw, "ssh_public_key", section_name)))
    priv = _expand(str(_require(raw, "ssh_private_key", section_name)))

    if not pub.exists():
        raise ConfigError(f"{section_name}.ssh_public_key does not exist: {pub}")
    if not priv.exists():
        raise ConfigError(f"{section_name}.ssh_private_key does not exist: {priv}")

    ssh_config = Path.home() / ".ssh" / "config"
    if "ssh_config" in raw:
        ssh_config = _expand(str(raw["ssh_config"]))

    extra_keys: list[Path] = []
    for entry in raw.get("extra_ssh_public_keys", []):
        p = _expand(str(entry))
        if not p.exists():
            raise ConfigError(f"{section_name}.extra_ssh_public_keys: file does not exist: {p}")
        extra_keys.append(p)

    host_prefix = str(raw.get("ssh_host_prefix", "awvm--"))
    if not SSH_HOST_PREFIX_RE.match(host_prefix):
        raise ConfigError(
            f"{section_name}.ssh_host_prefix must be alphanumeric with hyphens, underscores, "
            f"or dots (no whitespace or special characters), got: {host_prefix!r}"
        )

    agent_host_prefix = str(raw.get("ssh_agent_host_prefix", "awagent--"))
    if not SSH_HOST_PREFIX_RE.match(agent_host_prefix):
        raise ConfigError(
            f"{section_name}.ssh_agent_host_prefix must be alphanumeric with hyphens, underscores, "
            f"or dots (no whitespace or special characters), got: {agent_host_prefix!r}"
        )

    return OperatorConfig(
        ssh_public_key=pub,
        ssh_private_key=priv,
        ssh_config=ssh_config,
        ssh_config_dir=bool(raw.get("ssh_config_dir", True)),
        ssh_host_prefix=host_prefix,
        ssh_agent_host_prefix=agent_host_prefix,
        extra_ssh_public_keys=extra_keys,
    )


def _load_paths(data: dict[str, object]) -> PathsConfig:
    raw = data.get("paths", {})
    if not isinstance(raw, dict):
        raise ConfigError("[paths] must be a table")
    defaults = PathsConfig()
    vm_ws = str(raw["vm_workspaces"]) if "vm_workspaces" in raw else defaults.vm_workspaces
    if "vscode_workspaces" in raw:
        vscode_ws = _expand(str(raw["vscode_workspaces"]))
    elif "code_workspaces" in raw:
        vscode_ws = _expand(str(raw["code_workspaces"]))
    else:
        vscode_ws = defaults.vscode_workspaces
    backups = _expand(str(raw["backups"])) if "backups" in raw else defaults.backups
    return PathsConfig(vm_workspaces=vm_ws, vscode_workspaces=vscode_ws, backups=backups)


_DEFAULTS_KEYS = {"platform", "vm_host"}


def _load_defaults(data: dict[str, object], issues: list[str]) -> DefaultsConfig:
    raw = data.get("defaults", {})
    if not isinstance(raw, dict):
        raise ConfigError("[defaults] must be a table")

    if "git_credentials" in raw:
        raise ConfigError(
            "defaults.git_credentials has been removed. Move git_credentials into "
            "[admin.config] and/or [agent.config]."
        )

    _warn_unexpected_keys(raw, _DEFAULTS_KEYS, "defaults", issues)

    platform = raw.get("platform")
    if platform is not None and platform not in VALID_PLATFORMS:
        raise ConfigError(f"defaults.platform must be one of {VALID_PLATFORMS}, got: {platform}")

    return DefaultsConfig(
        platform=str(platform) if platform is not None else None,
        vm_host=str(raw["vm_host"]) if "vm_host" in raw else None,
    )


_NAMED_CONSOLE_KEYS = {"tmux_layout"}


def _load_named_console(
    data: dict[str, object],
    issues: list[str],
    decls: _SectionLineMap,
) -> NamedConsoleConfig:
    raw = data.get("named_console", {})
    if not isinstance(raw, dict):
        raise ConfigError("[named_console] must be a table")

    _warn_unexpected_keys(raw, _NAMED_CONSOLE_KEYS, "named_console", issues)

    layout = raw.get("tmux_layout", AW_SESSION_VERTICAL_LAYOUT)
    if layout not in VALID_TMUX_LAYOUTS:
        raise ConfigError(
            f"named_console.tmux_layout must be one of {VALID_TMUX_LAYOUTS}, "
            f"got: {layout}"
        )

    return NamedConsoleConfig(
        tmux_layout=str(layout),
        declared_at=decls.lookup("named_console"),
    )


_VM_TEMPLATE_KEYS = {
    "inherits",
    "cpus",
    "memory",
    "disk",
    "azure_vm_size",
    "swap",
    "apt",
    "apt_packages",
    "snap",
    "system_install_commands",
    "env",
    "tailscale_auth_key",
}


def _load_vm_templates(
    data: dict[str, object],
    issues: list[str],
    decls: _SectionLineMap,
) -> dict[str, VMTemplate]:
    raw = data.get("vm_templates", {})
    if not isinstance(raw, dict):
        raise ConfigError("[vm_templates] must be a table")

    if "vm" in data and isinstance(data["vm"], dict) and "config" in data["vm"]:
        raise ConfigError(
            "[vm.config] has been replaced by [vm_templates.default]."
        )

    # TOML's implicit-parent semantics already populate this dict: writing
    # `[vm_templates.x.env]` alone produces `raw == {"x": {"env": {...}}}` even
    # without a separate `[vm_templates.x]` header, and the loop iterates `x`
    # like any other template. Per the revised FRD R2, that minimal form is a
    # valid Resource; declared_at picks the earliest contributing header (the
    # env line, in that case) via `_SectionLineMap.lookup`.
    templates: dict[str, VMTemplate] = {}
    for name, tdata in raw.items():
        if not isinstance(tdata, dict):
            raise ConfigError(f"vm_templates.{name} must be a table")
        _warn_unexpected_keys(tdata, _VM_TEMPLATE_KEYS, f"vm_templates.{name}", issues)

        # tailscale_auth_key must be a non-empty bare string secret
        # name; no `{ secret = "..." }` polymorphism per the SDD (the
        # field IS the secret reference, not a value-or-ref
        # discriminator). Absence means "inherit" via the raw-template's
        # None sentinel; the resolver applies the default
        # ``"tailscale-auth-key"`` when no ancestor set it explicitly.
        # Empty-string is rejected: it would derive an env-var name
        # ``AW_SECRET_`` (empty suffix) and prompt the operator for a
        # secret called ``""`` -- a usability footgun on a security-
        # relevant field.
        ts_key_raw: str | None = None
        if "tailscale_auth_key" in tdata:
            if not isinstance(tdata["tailscale_auth_key"], str):
                raise ConfigError(
                    f"vm_templates.{name}.tailscale_auth_key must be a bare secret "
                    f"name (string), got {type(tdata['tailscale_auth_key']).__name__}"
                )
            if not tdata["tailscale_auth_key"]:
                raise ConfigError(
                    f"vm_templates.{name}.tailscale_auth_key must not be empty; "
                    f"omit the key to inherit the default secret name "
                    f"\"tailscale-auth-key\""
                )
            ts_key_raw = tdata["tailscale_auth_key"]

        templates[name] = VMTemplate(
            name=name,
            inherits=list(tdata.get("inherits", [])),
            cpus=int(tdata["cpus"]) if "cpus" in tdata else None,
            memory=int(tdata["memory"]) if "memory" in tdata else None,
            disk=int(tdata["disk"]) if "disk" in tdata else None,
            azure_vm_size=str(tdata["azure_vm_size"]) if "azure_vm_size" in tdata else None,
            swap=int(tdata["swap"]) if "swap" in tdata else None,
            apt=list(tdata["apt"]) if "apt" in tdata else None,
            apt_packages=list(tdata["apt_packages"]) if "apt_packages" in tdata else None,
            snap=list(tdata["snap"]) if "snap" in tdata else None,
            system_install_commands=(
                list(tdata["system_install_commands"]) if "system_install_commands" in tdata else None
            ),
            tailscale_auth_key=ts_key_raw,
            env=_parse_env_table(tdata.get("env"), context=f"vm_templates.{name}", issues=issues),
            declared_at=decls.lookup("vm_templates", name),
        )

    # Phase 2a.1: validation moved to the framework / resolver. The
    # framework's VMTemplateKind miss policy + Registry.finalize cycle
    # pass own the canonical inherits-reference validation (called via
    # build_registry). The per-template field-merging resolver in
    # agentworks.vms.templates also has its own visited-set cycle guard
    # for the load-time eager-resolve path. Either pass catches malformed
    # configs; this loader no longer does a pre-pass.

    return templates


_USER_CONFIG_KEYS = {
    "username",
    "shell",
    "git_credentials",
    "user_install_commands",
    "dotfiles_source",
    "dotfiles_destination",
    "dotfiles_install_cmd",
    "mise_activate",
    "mise_packages",
    "mise_lockfile",
    "mise_allow_unlocked",
    "mise_install_before",
    "mise_prune_on_reinit",
    "git_force_safe_directory",
    "claude_marketplaces",
    "claude_plugins",
}


def _load_admin_config(
    data: dict[str, object],
    issues: list[str],
    decls: _SectionLineMap,
) -> AdminConfig:
    """Load admin per-user config from [admin.config]."""
    top = data.get("admin", {})
    if not isinstance(top, dict):
        raise ConfigError("[admin] must be a table")
    raw = top.get("config", {})
    if not isinstance(raw, dict):
        raise ConfigError("[admin.config] must be a table")

    _warn_unexpected_keys(raw, _USER_CONFIG_KEYS, "admin.config", issues)

    return AdminConfig(
        username=str(raw.get("username", "agentworks")),
        shell=str(raw.get("shell", "bash")),
        git_credentials=list(raw.get("git_credentials", [])),
        user_install_commands=list(raw.get("user_install_commands", [])),
        dotfiles_source=str(raw["dotfiles_source"]) if "dotfiles_source" in raw else None,
        dotfiles_destination=str(raw.get("dotfiles_destination", "~/.dotfiles")),
        dotfiles_install_cmd=str(raw.get("dotfiles_install_cmd", "./install.sh")),
        mise_activate=bool(raw.get("mise_activate", True)),
        mise_packages=list(raw.get("mise_packages", [])),
        mise_lockfile=str(raw["mise_lockfile"]) if "mise_lockfile" in raw else None,
        mise_allow_unlocked=bool(raw.get("mise_allow_unlocked", False)),
        mise_install_before=str(raw.get("mise_install_before", "7d")),
        mise_prune_on_reinit=bool(raw.get("mise_prune_on_reinit", True)),
        git_force_safe_directory=bool(raw.get("git_force_safe_directory", True)),
        claude_marketplaces=_require_string_list(raw, "claude_marketplaces", "admin.config"),
        claude_plugins=_require_string_list(raw, "claude_plugins", "admin.config"),
        env=_parse_env_table(top.get("env"), context="admin", issues=issues),
        declared_at=decls.lookup("admin"),
    )


_AGENT_TEMPLATE_KEYS = _USER_CONFIG_KEYS | {"inherits", "env"}


def _load_agent_templates(
    data: dict[str, object],
    issues: list[str],
    decls: _SectionLineMap,
) -> dict[str, AgentTemplate]:
    raw = data.get("agent_templates", {})
    if not isinstance(raw, dict):
        raise ConfigError("[agent_templates] must be a table")

    if "agent" in data and isinstance(data["agent"], dict) and "config" in data["agent"]:
        raise ConfigError(
            "[agent.config] has been replaced by [agent_templates.default]."
        )

    templates: dict[str, AgentTemplate] = {}
    for name, tdata in raw.items():
        if not isinstance(tdata, dict):
            raise ConfigError(f"agent_templates.{name} must be a table")
        _warn_unexpected_keys(tdata, _AGENT_TEMPLATE_KEYS, f"agent_templates.{name}", issues)

        templates[name] = AgentTemplate(
            name=name,
            inherits=list(tdata.get("inherits", [])),
            shell=str(tdata["shell"]) if "shell" in tdata else None,
            git_credentials=list(tdata["git_credentials"]) if "git_credentials" in tdata else None,
            user_install_commands=(list(tdata["user_install_commands"]) if "user_install_commands" in tdata else None),
            dotfiles_source=str(tdata["dotfiles_source"]) if "dotfiles_source" in tdata else None,
            dotfiles_destination=(str(tdata["dotfiles_destination"]) if "dotfiles_destination" in tdata else None),
            dotfiles_install_cmd=(str(tdata["dotfiles_install_cmd"]) if "dotfiles_install_cmd" in tdata else None),
            mise_activate=bool(tdata["mise_activate"]) if "mise_activate" in tdata else None,
            mise_packages=list(tdata["mise_packages"]) if "mise_packages" in tdata else None,
            mise_lockfile=str(tdata["mise_lockfile"]) if "mise_lockfile" in tdata else None,
            mise_allow_unlocked=(bool(tdata["mise_allow_unlocked"]) if "mise_allow_unlocked" in tdata else None),
            mise_install_before=(str(tdata["mise_install_before"]) if "mise_install_before" in tdata else None),
            mise_prune_on_reinit=(bool(tdata["mise_prune_on_reinit"]) if "mise_prune_on_reinit" in tdata else None),
            claude_marketplaces=(
                _require_string_list(tdata, "claude_marketplaces", f"agent_templates.{name}")
                if "claude_marketplaces" in tdata else None
            ),
            claude_plugins=(
                _require_string_list(tdata, "claude_plugins", f"agent_templates.{name}")
                if "claude_plugins" in tdata else None
            ),
            env=_parse_env_table(tdata.get("env"), context=f"agent_templates.{name}", issues=issues),
            declared_at=decls.lookup("agent_templates", name),
        )

    # Phase 2a.2: inherits-reference validation and cycle detection move
    # to the framework (AgentTemplateKind's miss policy +
    # Registry.finalize's cycle pass). The agents/templates.py resolver
    # also has its own visited-set guard as a safety net for callers
    # that resolve without going through build_registry.
    return templates


def _load_catalog_sections(
    data: dict[str, object],
) -> tuple[
    dict[str, object],
    dict[str, object],
    dict[str, object],
    dict[str, object],
]:
    """Load the four user-defined catalog sections as raw dicts.

    Actual parsing into typed entries happens in catalog.py during merge.
    Here we just validate that each section is a table of tables.
    """
    sections = {}
    for section_name in ("apt_sources", "apt_packages", "system_install_commands", "user_install_commands"):
        raw = data.get(section_name, {})
        if not isinstance(raw, dict):
            raise ConfigError(f"[{section_name}] must be a table")
        for name, entry in raw.items():
            if not isinstance(entry, dict):
                raise ConfigError(f"{section_name}.{name} must be a table")
        sections[section_name] = raw
    return (
        sections["apt_sources"],
        sections["apt_packages"],
        sections["system_install_commands"],
        sections["user_install_commands"],
    )


def _load_workspace_templates(
    data: dict[str, object],
    issues: list[str],
    decls: _SectionLineMap,
) -> dict[str, WorkspaceTemplate]:
    raw = data.get("workspace_templates", {})
    if not isinstance(raw, dict):
        raise ConfigError("[workspace_templates] must be a table")

    templates: dict[str, WorkspaceTemplate] = {}
    for name, tdata in raw.items():
        if not isinstance(tdata, dict):
            raise ConfigError(f"workspace_templates.{name} must be a table")
        templates[name] = WorkspaceTemplate(
            name=name,
            inherits=list(tdata.get("inherits", [])),
            repo=str(tdata["repo"]) if "repo" in tdata else None,
            tmuxinator=bool(tdata["tmuxinator"]) if "tmuxinator" in tdata else None,
            env=_parse_env_table(
                tdata.get("env"),
                context=f"workspace_templates.{name}",
                issues=issues,
            ),
            declared_at=decls.lookup("workspace_templates", name),
        )

    # Phase 2a.2: inherits-reference validation and cycle detection move
    # to the framework (WorkspaceTemplateKind's miss policy +
    # Registry.finalize's cycle pass). The workspaces/templates.py
    # resolver also has its own visited-set guard.
    return templates


def _load_git_credentials(
    data: dict[str, object],
    issues: list[str],
    decls: _SectionLineMap,
) -> dict[str, GitCredentialConfig]:
    raw = data.get("git_credentials", {})
    if not isinstance(raw, dict):
        raise ConfigError("[git_credentials] must be a table")

    creds: dict[str, GitCredentialConfig] = {}
    for name, cdata in raw.items():
        if not isinstance(cdata, dict):
            raise ConfigError(f"git_credentials.{name} must be a table")
        # Phase 2b.1: the ``type`` field's reference-existence check
        # moves to the framework via
        # ``GitCredentialConfig.referenced_resources`` emitting a
        # ``ResourceReference(kind="git-credential-provider", ...)``;
        # ``_GitCredentialProviderKind``'s error miss policy fires at
        # build_registry time with the framework's consistent error
        # shape if the type isn't a known provider.
        # ``provider`` is the vocabulary going forward (matching
        # secret-backend manifests); ``type`` remains accepted until the
        # TOML resource surface is deleted at the cutover. ``provider``
        # wins when both are present.
        if "provider" in cdata:
            cred_type = str(cdata["provider"])
            if "type" in cdata and str(cdata["type"]) != cred_type:
                issues.append(
                    f"git_credentials.{name}: both provider ({cred_type!r}) "
                    f"and type ({cdata['type']!r}) are set and disagree; "
                    "provider wins"
                )
        elif "type" in cdata:
            cred_type = str(cdata["type"])
        else:
            raise ConfigError(f"git_credentials.{name}.provider is required")
        if cred_type == "azdo" and "org" not in cdata:
            raise ConfigError(f"git_credentials.{name}.org is required for azdo type")
        # (TOML keeps org at the section top level -- the only flat
        # domain; it nests into provider_config below.)

        # ``token`` is a bare secret name; same rules as
        # ``tailscale_auth_key``. Omission triggers GitCredentialConfig's
        # ``__post_init__`` default (``git-token-<name>``). Empty-string
        # is rejected so an operator who types ``token = ""`` doesn't
        # silently get a default-named secret behind their back.
        token_raw: str = ""
        if "token" in cdata:
            if not isinstance(cdata["token"], str):
                raise ConfigError(
                    f"git_credentials.{name}.token must be a bare secret "
                    f"name (string), got {type(cdata['token']).__name__}"
                )
            if not cdata["token"]:
                raise ConfigError(
                    f"git_credentials.{name}.token must not be empty; "
                    f"omit the key to inherit the default secret name "
                    f"\"git-token-{name}\""
                )
            token_raw = cdata["token"]

        provider_config: dict[str, object] = {}
        if "org" in cdata:
            provider_config["org"] = str(cdata["org"])
        creds[name] = GitCredentialConfig(
            name=name,
            provider=cred_type,
            provider_config=provider_config,
            description=str(cdata["description"]) if "description" in cdata else None,
            token=token_raw,
            declared_at=decls.lookup("git_credentials", name),
        )
    return creds


_SESSION_CONFIG_KEYS = {"history_limit"}


def _load_session_config(data: dict[str, object], issues: list[str]) -> SessionConfig:
    session_section = data.get("session", {})
    if not isinstance(session_section, dict):
        raise ConfigError("[session] must be a table")
    raw = session_section.get("config", {})
    if not isinstance(raw, dict):
        raise ConfigError("[session.config] must be a table")

    _warn_unexpected_keys(raw, _SESSION_CONFIG_KEYS, "session.config", issues)

    history_limit = int(raw.get("history_limit", 50_000))
    if history_limit < 1:
        raise ConfigError("session.config.history_limit must be a positive integer")

    return SessionConfig(
        history_limit=history_limit,
    )


_SESSION_TEMPLATE_KEYS = {"inherits", "command", "description", "restart_command", "required_commands", "env"}


def _load_session_templates(
    data: dict[str, object],
    issues: list[str],
    decls: _SectionLineMap,
) -> dict[str, SessionTemplate]:
    raw = data.get("session_templates", {})
    if not isinstance(raw, dict):
        raise ConfigError("[session_templates] must be a table")

    templates: dict[str, SessionTemplate] = {}
    for name, tdata in raw.items():
        if not isinstance(tdata, dict):
            raise ConfigError(f"session_templates.{name} must be a table")
        _warn_unexpected_keys(tdata, _SESSION_TEMPLATE_KEYS, f"session_templates.{name}", issues)
        env: dict[str, EnvEntry] | None = None
        if "env" in tdata:
            env = _parse_env_table(
                tdata["env"],
                context=f"session_templates.{name}",
                issues=issues,
            )
        templates[name] = SessionTemplate(
            name=name,
            inherits=list(tdata.get("inherits", [])),
            command=str(tdata["command"]) if "command" in tdata else None,
            description=str(tdata["description"]) if "description" in tdata else None,
            restart_command=str(tdata["restart_command"]) if "restart_command" in tdata else None,
            required_commands=(
                _require_string_list(tdata, "required_commands", f"session_templates.{name}")
                if "required_commands" in tdata else None
            ),
            env=env,
            declared_at=decls.lookup("session_templates", name),
        )

    # Phase 2a.2: inherits-reference validation and cycle detection move
    # to the framework (SessionTemplateKind's miss policy +
    # Registry.finalize's cycle pass). The sessions/templates.py
    # resolver also has its own visited-set guard.
    return templates


def _load_azure(data: dict[str, object]) -> AzureConfig | None:
    raw = data.get("azure")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ConfigError("[azure] must be a table")
    return AzureConfig(
        subscription_id=str(_require(raw, "subscription_id", "azure")),
        resource_group=str(_require(raw, "resource_group", "azure")),
        region=str(_require(raw, "region", "azure")),
        idle_timeout_hours=int(raw.get("idle_timeout_hours", 2)),
    )


def _load_proxmox(data: dict[str, object]) -> ProxmoxConfig | None:
    raw = data.get("proxmox")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ConfigError("[proxmox] must be a table")
    return ProxmoxConfig(
        api_url=str(_require(raw, "api_url", "proxmox")),
        node=str(_require(raw, "node", "proxmox")),
        token_id=str(_require(raw, "token_id", "proxmox")),
        template_vmid=int(str(_require(raw, "template_vmid", "proxmox"))),
        storage=str(raw.get("storage", "local-lvm")),
        bridge=str(raw.get("bridge", "vmbr0")),
        pool=str(raw.get("pool", "agentworks")),
        verify_ssl=bool(raw.get("verify_ssl", True)),
    )


def _load_secrets(
    data: dict[str, object],
    issues: list[str],
    decls: _SectionLineMap,
) -> dict[str, SecretDecl]:
    """Load [secrets.*] declarations into SecretDecls keyed by name."""
    raw = data.get("secrets", {})
    if not isinstance(raw, dict):
        raise ConfigError("[secrets] must be a table")

    expected = {"description", "hint", "backend_mappings"}
    secret_decls: dict[str, SecretDecl] = {}
    for name, sdata in raw.items():
        name_str = str(name)
        if not isinstance(sdata, dict):
            raise ConfigError(f"secrets.{name_str} must be a table")
        validate_name(name_str)
        _warn_unexpected_keys(sdata, expected, f"secrets.{name_str}", issues)

        description = sdata.get("description")
        if not isinstance(description, str) or not description:
            raise ConfigError(
                f"secrets.{name_str}.description is required and must be a non-empty string"
            )
        hint = sdata.get("hint")
        if hint is not None and not isinstance(hint, str):
            raise ConfigError(f"secrets.{name_str}.hint must be a string")

        raw_mappings = sdata.get("backend_mappings", {})
        if not isinstance(raw_mappings, dict):
            raise ConfigError(
                f"secrets.{name_str}.backend_mappings must be a table"
            )
        backend_mappings: dict[str, str | dict[str, object] | Literal[False]] = {}
        for kind, mapping in raw_mappings.items():
            kind_str = str(kind)
            if isinstance(mapping, bool):
                if mapping is True:
                    raise ConfigError(
                        f"secrets.{name_str}.backend_mappings.{kind_str}: "
                        "boolean must be `false` (opt-out); `true` is not a valid value"
                    )
                backend_mappings[kind_str] = False
            elif isinstance(mapping, str):
                backend_mappings[kind_str] = mapping
            elif isinstance(mapping, dict):
                backend_mappings[kind_str] = dict(mapping)
            else:
                raise ConfigError(
                    f"secrets.{name_str}.backend_mappings.{kind_str}: "
                    "must be a string, inline table, or false"
                )

        secret_decls[name_str] = SecretDecl(
            name=name_str,
            description=description,
            hint=hint,
            backend_mappings=backend_mappings,
            declared_at=decls.lookup("secrets", name_str),
        )
    return secret_decls


def _load_secret_backends(
    data: dict[str, object],
    deprecations: list[str],
) -> None:
    """Warn ``[secret_backends.*]`` sections as deprecated no-ops.

    The kind-keyed TOML sections never carried configuration (only the
    kind itself), and the built-in backends now ship as bundled
    manifests -- so a section here is semantically empty. Known kinds
    warn as deprecated; unknown kinds (typo ``envvar`` for ``env-var``)
    stay a hard ``ConfigError`` for typo protection. Nothing is stored
    and nothing publishes.
    """
    raw = data.get("secret_backends", {})
    if not isinstance(raw, dict):
        raise ConfigError("[secret_backends] must be a table")

    from agentworks.secrets.providers import SECRET_PROVIDER_REGISTRY

    known_providers = set(SECRET_PROVIDER_REGISTRY)
    for key, bdata in raw.items():
        provider_str = str(key)
        if not isinstance(bdata, dict):
            raise ConfigError(f"secret_backends.{provider_str} must be a table")
        if provider_str not in known_providers:
            raise ConfigError(
                f"[secret_backends.{provider_str}] names an unknown secret "
                f"provider; supported: {sorted(known_providers)}"
            )
        deprecations.append(
            f"[secret_backends.{provider_str}] is deprecated and has no effect: "
            f"the built-in backends ship with agentworks, and activation is "
            f"[secret_config].backends. Remove the section, or run "
            f"`agw resource migrate --all` to drop it."
        )


def _warn_deprecated_resource_sections(
    data: dict[str, object],
    deprecations: list[str],
) -> None:
    """ONE aggregated deprecation issue for the TOML resource sections
    present (Phase 5, aggregated at maintainer direction -- a warning
    per section was obnoxious on real configs).

    Dual-path is permanent policy short of a future major release: these
    sections keep loading with exactly today's semantics. The warning is
    the nudge toward the YAML manifest surface. ``[secret_backends.*]``
    is excluded -- it has its own no-op message above -- and
    ``[secret_config]`` is config, not a resource section.
    """
    from agentworks.manifests.decode import KIND_SECTIONS

    present: list[str] = []
    for _kind, section in KIND_SECTIONS.items():
        if section == "secret_backends" or section not in data:
            continue
        # Display the header shape operators can actually grep for:
        # [admin.config] and [named_console] are the two non-family
        # sections; everything else nests names ([secrets.<name>]).
        if section == "admin":
            present.append("[admin.config]")
        elif section == "named_console":
            present.append("[named_console]")
        else:
            present.append(f"[{section}.*]")
    if not present:
        return
    noun = "section" if len(present) == 1 else "sections"
    deprecations.append(
        f"deprecated TOML resource {noun}: {', '.join(present)}. Declare "
        f"new resources as YAML manifests (`agw resource sample <kind>`), "
        f"move these with `agw resource migrate` (per kind, or --all), or "
        f"silence this warning with --no-deprecations. TOML resource "
        f"support will likely be removed in a future major release."
    )


def _load_secret_config(
    data: dict[str, object],
    issues: list[str],
    decls: _SectionLineMap,
) -> SecretConfig:
    """Load [secret_config] with the enabled-backends precedence list.

    Absence of the [secret_config] table OR absence of the ``backends`` key
    within it falls back to ``SecretConfig()``'s default chain
    (``DEFAULT_BACKEND_CHAIN``). An explicit ``backends = []`` is respected
    as "no backends" (operator opts out of resolution entirely).
    """
    declared_at = decls.lookup("secret_config")
    if "secret_config" not in data:
        return SecretConfig(declared_at=declared_at)
    raw = data["secret_config"]
    if not isinstance(raw, dict):
        raise ConfigError("[secret_config] must be a table")
    _warn_unexpected_keys(raw, {"backends"}, "secret_config", issues)
    if "backends" not in raw:
        return SecretConfig(declared_at=declared_at)
    backends_raw = raw["backends"]
    if not isinstance(backends_raw, list) or not all(
        isinstance(b, str) for b in backends_raw
    ):
        raise ConfigError("[secret_config].backends must be a list of strings")
    return SecretConfig(backends=tuple(backends_raw), declared_at=declared_at)


# Secret resolution lives in ``agentworks.secrets.resolve`` (ADR 0016):
# the chain can name manifest-declared backends, which are unknowable at
# config-load time, so the chain-name and unreachable-secret checks run
# at the composition boundary instead of here.


EXPECTED_TOP_LEVEL_KEYS = {
    "operator",
    "paths",
    "defaults",
    "named_console",
    "vm_templates",
    "admin",
    "agent_templates",
    "session",
    "session_templates",
    "apt_sources",
    "apt_packages",
    "system_install_commands",
    "user_install_commands",
    "workspace_templates",
    "git_credentials",
    "azure",
    "proxmox",
    "secrets",
    "secret_backends",
    "secret_config",
}


def _warn_unexpected_top_level_keys(data: dict[str, object], issues: list[str]) -> None:
    """Record unexpected top-level keys.

    This catches a common TOML pitfall: uncommenting a key without its section
    header causes the key to land in the wrong (or top-level) section.
    """
    unexpected = set(data.keys()) - EXPECTED_TOP_LEVEL_KEYS
    if unexpected:
        keys = ", ".join(sorted(unexpected))
        issues.append(f"unexpected top-level keys in config: {keys}")


def load_config(path: Path | None = None, *, warn_issues: bool = True) -> Config:
    """Load and validate the agentworks configuration.

    Args:
        path: Override config file path (default: ~/.config/agentworks/config.toml).
        warn_issues: Emit config issues as warnings to stderr (default: True).
            Set to False when the caller handles issues itself (e.g. doctor).

    Returns:
        Validated Config object.

    Raises:
        ConfigError: If the config is missing or invalid.
        SystemExit: If the config file does not exist.
    """
    config_path = path or CONFIG_PATH
    if not config_path.exists():
        print(f"Configuration file not found: {config_path}", file=sys.stderr)
        print("Create it to get started. See the documentation for the schema.", file=sys.stderr)
        raise SystemExit(1)

    raw_text = config_path.read_text()
    try:
        data = tomllib.loads(raw_text)
    except tomllib.TOMLDecodeError as e:
        print(f"Error: invalid config file {config_path}: {e}", file=sys.stderr)
        raise SystemExit(1) from None

    # Pre-scan the raw text for section-header line numbers so we can attach
    # ``declared_at: SourceLocation`` to every composed Resource. tomllib loses
    # this info on parse; the scanner is a small regex pre-pass.
    decls = _SectionLineMap(
        config_path=config_path,
        section_lines=scan_section_lines(raw_text),
    )

    issues: list[str] = []

    _warn_unexpected_top_level_keys(data, issues)

    if "dotfiles" in data:
        raise ConfigError(
            "[dotfiles] section has been removed. Move dotfiles settings into "
            "[admin.config] (dotfiles_source, dotfiles_destination, dotfiles_install_cmd)."
        )

    git_credentials = _load_git_credentials(data, issues, decls)
    apt_sources, apt_packages, system_cmds, user_cmds = _load_catalog_sections(data)

    session_config = _load_session_config(data, issues)
    session_templates = _load_session_templates(data, issues, decls)

    loaded_vm_templates = _load_vm_templates(data, issues, decls)
    loaded_agent_templates = _load_agent_templates(data, issues, decls)


    admin = _load_admin_config(data, issues, decls)
    workspace_templates = _load_workspace_templates(data, issues, decls)

    secrets = _load_secrets(data, issues, decls)
    deprecations: list[str] = []
    _load_secret_backends(data, deprecations)
    _warn_deprecated_resource_sections(data, deprecations)
    secret_config_data = _load_secret_config(data, issues, decls)
    # Phase 1b: env-block secret references no longer error at config load
    # when they don't match a [secrets.<name>] block; the framework
    # auto-declares them at finalize. Resolution runs through the active
    # backends (``agentworks.secrets.resolve``).

    config = Config(
        operator=_load_operator(data, issues),
        paths=_load_paths(data),
        defaults=_load_defaults(data, issues),
        named_console=_load_named_console(data, issues, decls),
        vm_templates=loaded_vm_templates,
        source_path=config_path,
        admin=admin,
        agent_templates=loaded_agent_templates,
        session=session_config,
        session_templates=session_templates,
        workspace_templates=workspace_templates,
        git_credentials=git_credentials,
        apt_sources=apt_sources,
        apt_packages=apt_packages,
        system_install_commands=system_cmds,
        user_install_commands=user_cmds,
        azure=_load_azure(data),
        proxmox=_load_proxmox(data),
        secrets=secrets,
        secret_config_data=secret_config_data,
        config_issues=tuple(issues),
        deprecation_issues=tuple(deprecations),
    )

    if warn_issues and config.config_issues:
        from agentworks.output import warn

        for issue in config.config_issues:
            warn(f"Config: {issue}")
    if warn_issues and config.deprecation_issues:
        from agentworks.output import deprecations_suppressed, warn

        if not deprecations_suppressed():
            for issue in config.deprecation_issues:
                warn(f"Config: {issue}")

    return config
