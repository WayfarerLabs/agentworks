"""Agentworks configuration loading and validation.

Config lives at ~/.config/agentworks/config.toml. It is read-only at runtime.
"""

from __future__ import annotations

import re
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol

# ConfigError is defined in agentworks.errors and re-exported here for backward
# compatibility with existing `from agentworks.config import ConfigError` users.
# The `X as X` shape marks the name as an explicit re-export for mypy strict mode.
from agentworks.env import EnvEntry
from agentworks.errors import ConfigError as ConfigError
from agentworks.secrets import (
    SecretBackendConfig,
    SecretConfig,
    SecretDecl,
)
from agentworks.source_location import SourceLocation, scan_section_lines, synthesized

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from agentworks.agents.templates import ResolvedAgentTemplate
    from agentworks.secrets import SecretResolver, SecretSource
    from agentworks.vms.templates import ResolvedVMTemplate

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


# Valid values for enum-like fields
VALID_PLATFORMS = ("lima", "azure", "wsl2", "proxmox")
VALID_GIT_CREDENTIAL_TYPES = ("azdo", "github")


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


@dataclass(frozen=True)
class NamedConsoleConfig:
    """Settings for the `console` subcommand group (named multi-session
    consoles). Section is `[named_console]` in the TOML to disambiguate from
    the legacy `vm console` and the workspace console template. Only named
    consoles read these values today.
    """

    tmux_layout: str = AW_SESSION_VERTICAL_LAYOUT
    declared_at: SourceLocation = field(default_factory=synthesized)


@dataclass(frozen=True)
class VMTemplate:
    """VM template definition. All fields are optional (None = inherit/default)."""

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
    declared_at: SourceLocation = field(default_factory=synthesized)


@dataclass(frozen=True)
class AdminConfig:
    """Per-user config for the admin user on VMs."""

    username: str = "agentworks"
    shell: str = "zsh"
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


@dataclass(frozen=True)
class WorkspaceTemplate:
    name: str
    inherits: list[str] = field(default_factory=list)
    repo: str | None = None
    tmuxinator: bool | None = None  # None = not explicitly set (inherit/default to True)
    env: dict[str, EnvEntry] = field(default_factory=dict)
    declared_at: SourceLocation = field(default_factory=synthesized)


@dataclass(frozen=True)
class GitCredentialConfig:
    name: str
    type: str
    org: str | None = None
    description: str | None = None
    declared_at: SourceLocation = field(default_factory=synthesized)


@dataclass(frozen=True)
class SessionTemplate:
    """Session template definition. All fields optional (None = inherit/default)."""

    name: str
    inherits: list[str] = field(default_factory=list)
    command: str | None = None
    description: str | None = None
    restart_command: str | None = None
    env: dict[str, EnvEntry] | None = None
    declared_at: SourceLocation = field(default_factory=synthesized)


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
    vm: ResolvedVMTemplate
    admin: AdminConfig
    agent_templates: dict[str, AgentTemplate]
    agent: ResolvedAgentTemplate
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
    secret_backends: dict[str, SecretBackendConfig] = field(default_factory=dict)
    # Top-level [secret_config] table; carries the enabled-backends precedence list.
    secret_config_data: SecretConfig = field(default_factory=SecretConfig)
    # Resolver assembled from secret_config_data.backends in precedence order.
    # An empty SecretResolver (no sources, no secrets) is constructed when the
    # operator hasn't opted into secrets - callers always get a usable resolver,
    # so call sites can `.render(env)` unconditionally instead of branching on
    # None. _validate_env_secret_refs runs before resolver assembly, so this
    # empty-chain shape never has to face a secret-ref env entry.
    secret_resolver: SecretResolver = field(default_factory=lambda: _empty_resolver())
    config_issues: tuple[str, ...] = ()


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
            # copy-paste artifact). The runtime resolver applies the
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

    templates: dict[str, VMTemplate] = {}
    for name, tdata in raw.items():
        if not isinstance(tdata, dict):
            raise ConfigError(f"vm_templates.{name} must be a table")
        _warn_unexpected_keys(tdata, _VM_TEMPLATE_KEYS, f"vm_templates.{name}", issues)

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
            env=_parse_env_table(tdata.get("env"), context=f"vm_templates.{name}", issues=issues),
            declared_at=decls.lookup("vm_templates", name),
        )

    # Validate inherits references and cycles
    for name, tmpl in templates.items():
        for parent in tmpl.inherits:
            if parent not in templates and parent != "default":
                raise ConfigError(f"vm_templates.{name}.inherits references unknown template: {parent}")
    _detect_template_cycles(templates, "vm_templates")

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
        shell=str(raw.get("shell", "zsh")),
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

    for name, tmpl in templates.items():
        for parent in tmpl.inherits:
            if parent not in templates and parent != "default":
                raise ConfigError(f"agent_templates.{name}.inherits references unknown template: {parent}")
    _detect_template_cycles(templates, "agent_templates")

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

    # validate inherits references and cycles
    for name, tmpl in templates.items():
        for parent in tmpl.inherits:
            if parent not in templates and parent != "default":
                raise ConfigError(f"workspace_templates.{name}.inherits references unknown template: {parent}")
    _detect_template_cycles(templates, "workspace_templates")

    return templates


class _HasInherits(Protocol):
    @property
    def inherits(self) -> list[str]: ...


def _detect_template_cycles(templates: Mapping[str, _HasInherits], label: str) -> None:
    visited: set[str] = set()
    in_stack: set[str] = set()

    def visit(name: str) -> None:
        if name not in templates:
            return  # implicit default or already validated
        if name in in_stack:
            raise ConfigError(f"{label} inheritance cycle detected involving: {name}")
        if name in visited:
            return
        in_stack.add(name)
        for parent in templates[name].inherits:
            visit(parent)
        in_stack.remove(name)
        visited.add(name)

    for name in templates:
        visit(name)


def _load_git_credentials(
    data: dict[str, object],
    decls: _SectionLineMap,
) -> dict[str, GitCredentialConfig]:
    raw = data.get("git_credentials", {})
    if not isinstance(raw, dict):
        raise ConfigError("[git_credentials] must be a table")

    creds: dict[str, GitCredentialConfig] = {}
    for name, cdata in raw.items():
        if not isinstance(cdata, dict):
            raise ConfigError(f"git_credentials.{name} must be a table")
        cred_type = str(_require(cdata, "type", f"git_credentials.{name}"))
        if cred_type not in VALID_GIT_CREDENTIAL_TYPES:
            raise ConfigError(
                f"git_credentials.{name}.type must be one of {VALID_GIT_CREDENTIAL_TYPES}, got: {cred_type}"
            )
        if cred_type == "azdo" and "org" not in cdata:
            raise ConfigError(f"git_credentials.{name}.org is required for azdo type")
        creds[name] = GitCredentialConfig(
            name=name,
            type=cred_type,
            org=str(cdata["org"]) if "org" in cdata else None,
            description=str(cdata["description"]) if "description" in cdata else None,
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


_SESSION_TEMPLATE_KEYS = {"inherits", "command", "description", "restart_command", "env"}


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
            env=env,
            declared_at=decls.lookup("session_templates", name),
        )

    for name, tmpl in templates.items():
        for parent in tmpl.inherits:
            if parent not in templates and parent != "default":
                raise ConfigError(f"session_templates.{name}.inherits references unknown template: {parent}")
    _detect_template_cycles(templates, "session_templates")

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
    issues: list[str],
    decls: _SectionLineMap,
) -> dict[str, SecretBackendConfig]:
    """Load [secret_backends.*] sections into SecretBackendConfig entries.

    v1 only carries the ``kind`` field; per-backend subclasses (with account /
    vault / etc.) arrive when those backends ship. Extra fields in v1
    sections are accepted but ignored.

    A declared kind not in the known-factory map (e.g. typo ``envvar`` for
    ``env-var``) emits a load-time warning so operators discover it before
    they reach for the section in ``[secret_config].backends``.
    """
    raw = data.get("secret_backends", {})
    if not isinstance(raw, dict):
        raise ConfigError("[secret_backends] must be a table")

    known_kinds = set(_v1_source_factories().keys())
    backends: dict[str, SecretBackendConfig] = {}
    for kind, bdata in raw.items():
        kind_str = str(kind)
        if not isinstance(bdata, dict):
            raise ConfigError(f"secret_backends.{kind_str} must be a table")
        if kind_str not in known_kinds:
            issues.append(
                f"[secret_backends.{kind_str}] declares an unknown backend kind; "
                f"v1 supports {sorted(known_kinds)}"
            )
        backends[kind_str] = SecretBackendConfig(
            kind=kind_str,
            declared_at=decls.lookup("secret_backends", kind_str),
        )
    return backends


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


def _empty_resolver() -> SecretResolver:
    """A no-op SecretResolver used as the default when no secrets are configured.

    Lets call sites depend on `Config.secret_resolver` always being a valid
    SecretResolver instead of branching on None. Safe because
    `_validate_env_secret_refs` runs before resolver assembly, so an empty
    chain never has to face a secret-ref env entry.
    """
    from agentworks.secrets import SecretResolver

    return SecretResolver([], {})


# Backend kinds whose source class is built in to v1. Source factories
# accept no constructor args today; later backends accepting a
# ``SecretBackendConfig`` will widen this signature when they ship.
def _v1_source_factories() -> dict[str, Callable[[], SecretSource]]:
    from agentworks.secrets import EnvVarSource, PromptSource

    return {
        "env-var": EnvVarSource,
        "prompt": PromptSource,
    }


def _build_secret_resolver(
    secret_config_data: SecretConfig,
    secret_backends: dict[str, SecretBackendConfig],  # noqa: ARG001 - reserved for future per-backend ctor wiring
    secrets: dict[str, SecretDecl],
) -> SecretResolver:
    """Assemble a SecretResolver from the configured backend chain.

    Returns a no-op resolver when no backends are configured (and no secrets
    are declared). When backends ARE configured, validates:

    - every kind in ``[secret_config].backends`` has a known source factory;
    - no declared secret is unreachable through the configured chain.
    """
    from agentworks.secrets import SecretResolver

    if not secret_config_data.backends and not secrets:
        return _empty_resolver()

    factories = _v1_source_factories()
    sources: list[SecretSource] = []
    for kind in secret_config_data.backends:
        factory = factories.get(kind)
        if factory is None:
            raise ConfigError(
                f"[secret_config].backends: unknown backend kind {kind!r}; "
                f"v1 supports {sorted(factories.keys())}"
            )
        sources.append(factory())

    resolver = SecretResolver(sources, secrets)

    unreachable = resolver.unreachable_secrets()
    if unreachable:
        names = ", ".join(sorted(d.name for d in unreachable))
        chain_str = ", ".join(secret_config_data.backends) or "(empty)"
        # The unreachable-secret case is tight by construction: with the
        # default chain (``env-var``, ``prompt``), prompt's would_attempt
        # returns True for every secret, so nothing is unreachable.
        # Reaching this error means the operator has either:
        # (a) explicitly stripped prompt from [secret_config].backends, AND
        # (b) the remaining backends opt out via backend_mappings (env-var
        #     respects `false`; backends without default conventions like
        #     1password require an explicit mapping), OR
        # (c) explicitly set backends = [] (resolution disabled).
        # The hint enumerates the three remediations in the order they're
        # most often the right fix.
        raise ConfigError(
            f"unreachable secret(s): {names}",
            hint=(
                f"active backend chain: [{chain_str}]. Each declared secret "
                "needs at least one backend in the chain that would attempt "
                "it. To fix: add 'prompt' (or another always-attempting backend) "
                "to [secret_config].backends; drop a "
                "`backend_mappings.<kind> = false` opt-out on the affected "
                "secret(s); add `backend_mappings.<kind>` for a backend that "
                "has no default convention (e.g. 1password); or remove the "
                "unused secret declaration."
            ),
        )

    return resolver


def _validate_env_secret_refs(
    *,
    secrets: dict[str, SecretDecl],
    admin: AdminConfig,
    vm_templates: dict[str, VMTemplate],
    workspace_templates: dict[str, WorkspaceTemplate],
    agent_templates: dict[str, AgentTemplate],
    session_templates: dict[str, SessionTemplate],
) -> None:
    """Verify every env entry's secret reference points at a declared secret."""

    def _check(env: dict[str, EnvEntry], context: str) -> None:
        for key, entry in env.items():
            if entry.secret is not None and entry.secret not in secrets:
                raise ConfigError(
                    f"{context}.{key} references undeclared secret "
                    f"{entry.secret!r}; declare it under [secrets.{entry.secret}]"
                )

    _check(admin.env, "admin.env")
    for tname, vt in vm_templates.items():
        _check(vt.env, f"vm_templates.{tname}.env")
    for tname, wt in workspace_templates.items():
        _check(wt.env, f"workspace_templates.{tname}.env")
    for tname, at in agent_templates.items():
        _check(at.env, f"agent_templates.{tname}.env")
    for tname, st in session_templates.items():
        if st.env:
            _check(st.env, f"session_templates.{tname}.env")


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

    git_credentials = _load_git_credentials(data, decls)
    apt_sources, apt_packages, system_cmds, user_cmds = _load_catalog_sections(data)

    session_config = _load_session_config(data, issues)
    session_templates = _load_session_templates(data, issues, decls)

    loaded_vm_templates = _load_vm_templates(data, issues, decls)
    loaded_agent_templates = _load_agent_templates(data, issues, decls)

    # Resolve default templates eagerly so config.vm / config.agent work everywhere
    from agentworks.vms.templates import resolve_from_dict as _resolve_vm

    resolved_vm = _resolve_vm(loaded_vm_templates)

    from agentworks.agents.templates import resolve_from_dict as _resolve_agent

    resolved_agent = _resolve_agent(loaded_agent_templates)

    admin = _load_admin_config(data, issues, decls)
    workspace_templates = _load_workspace_templates(data, issues, decls)

    secrets = _load_secrets(data, issues, decls)
    secret_backends = _load_secret_backends(data, issues, decls)
    secret_config_data = _load_secret_config(data, issues, decls)
    _validate_env_secret_refs(
        secrets=secrets,
        admin=admin,
        vm_templates=loaded_vm_templates,
        workspace_templates=workspace_templates,
        agent_templates=loaded_agent_templates,
        session_templates=session_templates,
    )
    secret_resolver = _build_secret_resolver(
        secret_config_data, secret_backends, secrets
    )

    config = Config(
        operator=_load_operator(data, issues),
        paths=_load_paths(data),
        defaults=_load_defaults(data, issues),
        named_console=_load_named_console(data, issues, decls),
        vm_templates=loaded_vm_templates,
        vm=resolved_vm,
        admin=admin,
        agent_templates=loaded_agent_templates,
        agent=resolved_agent,
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
        secret_backends=secret_backends,
        secret_config_data=secret_config_data,
        secret_resolver=secret_resolver,
        config_issues=tuple(issues),
    )

    if warn_issues and config.config_issues:
        from agentworks.output import warn

        for issue in config.config_issues:
            warn(f"Config: {issue}")

    return config
