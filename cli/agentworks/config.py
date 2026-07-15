"""Agentworks configuration loading and validation.

Config lives at ~/.config/agentworks/config.toml. It is read-only at runtime.

Post-move contract (resource-manifests SDD): this module holds the
settings dataclasses plus the legacy TOML resource loaders/publisher
(``Config.publish_to`` and the ``_load_*`` helpers) that die in Phase 6 --
nothing else. The declarable-resource dataclasses (VMTemplate,
AgentTemplate, AdminConfig, WorkspaceTemplate, SessionTemplate,
NamedConsoleConfig, GitCredentialConfig) and the console/tmux layout
constants now live in their domain packages; the loaders import them from
there. Kind definitions live in the domain packages too (see
``agentworks.resources.kinds`` for the registration index).
"""

from __future__ import annotations

import re
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from agentworks.agents.template import AgentTemplate

# ConfigError is defined in agentworks.errors and re-exported here for backward
# compatibility with existing `from agentworks.config import ConfigError` users.
# The `X as X` shape marks the name as an explicit re-export for mypy strict mode.
from agentworks.env import EnvEntry
from agentworks.errors import ConfigError as ConfigError
from agentworks.git_credentials.credential import GitCredentialConfig
from agentworks.secrets import (
    SecretConfig,
    SecretDecl,
)
from agentworks.sessions.layouts import AW_SESSION_VERTICAL_LAYOUT, VALID_TMUX_LAYOUTS
from agentworks.sessions.template import NamedConsoleConfig, SessionTemplate
from agentworks.source_location import SourceLocation, scan_section_lines
from agentworks.vms.admin import AdminConfig
from agentworks.vms.template import VMTemplate
from agentworks.workspaces.template import WorkspaceTemplate

if TYPE_CHECKING:

    from agentworks.resources.origin import Origin
    from agentworks.resources.registry import Registry
    from agentworks.vms.sites import VMSiteDecl

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


# https/http git URL with embedded userinfo ("https://user@host/...").
_HTTP_USERINFO_RE = re.compile(r"^https?://[^/@]+@")


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


_DEFAULTS_KEYS = {"site", "platform", "runup_git_credentials"}


def _load_defaults(
    data: dict[str, object],
    issues: list[str],
    deprecations: list[str],
) -> DefaultsConfig:
    raw = data.get("defaults", {})
    if not isinstance(raw, dict):
        raise ConfigError("[defaults] must be a table")

    if "git_credentials" in raw:
        raise ConfigError(
            "defaults.git_credentials has been removed. Move git_credentials into "
            "[admin.config] and/or [agent.config]."
        )

    if "vm_host" in raw:
        # No alias is possible: the replacement is a vm-site manifest
        # only the operator can author (the old vm-host registry that
        # mapped this name to an SSH target is gone). The old value was
        # the host's NAME, which doubles as the natural site name; the
        # operator supplies the SSH target in platform_config.vm_host.
        from agentworks.vms.sites import site_manifest_hint

        old_name = str(raw["vm_host"])
        raise ConfigError(
            "defaults.vm_host has been removed; remote Lima hosts are "
            "vm-site resources now",
            hint=(
                site_manifest_hint(old_name, vm_host="<user@host>")
                + "\n\nthen set defaults.site to the site's name"
            ),
        )

    _warn_unexpected_keys(raw, _DEFAULTS_KEYS, "defaults", issues)

    # `site` names a vm-site resource; existence is validated at the
    # composition boundary (vms.validate_sites), where the finalized
    # registry knows every declared site. `platform` is the retired
    # spelling, accepted as a one-release deprecated alias; its old
    # values name the built-in and legacy-TOML sites, so the value
    # carries over, with one translation: the old `lima` meant local
    # Lima, whose bundled site is now named `lima-local`.
    site = raw.get("site")
    if site is not None and (not isinstance(site, str) or not site):
        raise ConfigError("defaults.site must be a non-empty site name")
    if "platform" in raw:
        alias = str(raw["platform"])
        if alias == "lima":
            alias = "lima-local"
        if site is not None:
            if alias != site:
                issues.append(
                    f"defaults: both site ({site!r}) and the deprecated "
                    f"platform alias ({raw['platform']!r}) are set and "
                    f"disagree; site wins"
                )
        else:
            site = alias
        deprecations.append(
            "defaults.platform is deprecated; rename the key to "
            "defaults.site (old value `lima` becomes `lima-local`, the "
            "bundled local-Lima site's new name; other values carry "
            "over unchanged). The alias will be removed in the next "
            "release."
        )

    return DefaultsConfig(
        site=str(site) if site is not None else None,
        runup_git_credentials=bool(raw.get("runup_git_credentials", True)),
    )


_NAMED_CONSOLE_KEYS = {"tmux_layout"}


def _load_named_console(
    data: dict[str, object],
    issues: list[str],
    decls: _SectionLineMap,
) -> NamedConsoleConfig | None:
    if "named_console" not in data:
        # Nothing declared: the framework auto-declares the default row
        # (always-materialize), same as every other reserved-default
        # kind. The manifest decoder always passes the key, so this
        # None path is TOML-only.
        return None
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
) -> AdminConfig | None:
    """Load admin per-user config from [admin.config].

    Returns ``None`` when the TOML has no ``[admin.*]`` sections at all:
    the framework auto-declares ``admin-template:default`` instead
    (always-materialize). The manifest decoder always passes the key.
    """
    if "admin" not in data:
        return None
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
        repo = str(tdata["repo"]) if "repo" in tdata else None
        if repo is not None and _HTTP_USERINFO_RE.match(repo):
            issues.append(
                f"workspace_templates.{name}.repo embeds a username; use a "
                f"plain https remote: git credential scoping selects "
                f"credentials automatically, and an embedded username "
                f"bypasses it"
            )
        templates[name] = WorkspaceTemplate(
            name=name,
            inherits=list(tdata.get("inherits", [])),
            repo=repo,
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
    *,
    warn_ignored_scope_keys: bool = True,
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
        # (TOML keeps org at the section top level -- the only flat
        # domain; it nests into provider_config below, and the provider
        # capability validates the assembled blob. Unknown provider
        # names defer to the framework's miss policy at finalize.)

        provider_config: dict[str, object] = {}
        # ``token`` is a bare secret name the provider sources its PAT
        # from. Flat in TOML, hoisted into provider_config so the
        # internal rep matches the YAML manifest shape (the provider's
        # validate_config owns the ``git-token-<name>`` default when it
        # is omitted). Empty-string is rejected so an operator who types
        # ``token = ""`` doesn't silently get the default behind their
        # back.
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
            provider_config["token"] = cdata["token"]
        # The flat TOML shape only ever read ``org``, and only for azdo;
        # hoisting it into the blob for other providers would promote a
        # historically-ignored stray key into a validation error and
        # break released configs (loads-today). The flat domain's
        # stray-key silence stays until Phase 6 retires it, EXCEPT
        # github scope keys, where silence would ship a credential with
        # BROADER authority than the operator declared; those warn.
        if warn_ignored_scope_keys and cred_type == "github":
            ignored_scopes = sorted({"repos", "owner"} & set(cdata))
            if ignored_scopes:
                issues.append(
                    f"git_credentials.{name}: github scope field(s) "
                    f"{', '.join(ignored_scopes)} are manifest-only and "
                    f"IGNORED here: the credential is provisioned "
                    f"unscoped; migrate it to YAML "
                    f"(agw resource migrate git-credential)"
                )
        if cred_type == "azdo" and "org" in cdata:
            provider_config["org"] = str(cdata["org"])
        from agentworks.capabilities.git_credential import (
            GIT_CREDENTIAL_PROVIDER_REGISTRY,
        )

        capability = GIT_CREDENTIAL_PROVIDER_REGISTRY.get(cred_type)
        if capability is not None:
            capability.validate_config(f"git-credential/{name}", provider_config)
        creds[name] = GitCredentialConfig(
            name=name,
            provider=cred_type,
            provider_config=provider_config,
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


# The flat legacy [azure] / [proxmox] keys that hoist into the nested
# platform_config blob. The flat domain stays silently loose on stray
# keys (the git-credential `org` precedent: promoting historically-
# ignored keys into validation errors would break released configs);
# manifests validate the true blob strictly.
_LEGACY_SITE_SECTIONS: dict[str, tuple[str, tuple[str, ...]]] = {
    # The section (and thus the declared SITE) keeps its historical
    # name "azure" (released configs and VM rows point at it), while
    # the platform underneath is azure-vm (the Azure Virtual Machines
    # service specifically).
    "azure": ("azure-vm", ("subscription_id", "resource_group", "region")),
    "proxmox": (
        "proxmox",
        (
            "api_url",
            "node",
            "token_id",
            "template_vmid",
            "storage",
            "bridge",
            "pool",
            "verify_ssl",
            "token_secret",
        ),
    ),
}


def _load_vm_sites_legacy(
    data: dict[str, object],
    decls: _SectionLineMap,
) -> dict[str, VMSiteDecl]:
    """Load the legacy ``[azure]`` / ``[proxmox]`` sections as ``vm-site``
    resources (dual-path; the sections warn as deprecated via
    ``_warn_deprecated_resource_sections``).

    Flat TOML is the one place platform-owned fields sit outside the
    ``platform_config`` blob; this loader nests at the boundary
    (section name becomes the site name, ``platform`` is synthesized),
    exactly as the git-credential loader nests ``org``. The platform
    capability validates the assembled blob so errors carry config
    vocabulary and the implied secret references derive at finalize.
    """
    from agentworks.capabilities.vm_platform import VM_PLATFORM_REGISTRY
    from agentworks.vms.sites import VMSiteDecl

    sites: dict[str, VMSiteDecl] = {}
    for section, (platform_name, known_keys) in _LEGACY_SITE_SECTIONS.items():
        raw = data.get(section)
        if raw is None:
            continue
        if not isinstance(raw, dict):
            raise ConfigError(f"[{section}] must be a table")
        platform_config: dict[str, object] = {
            key: raw[key] for key in known_keys if key in raw
        }
        capability = VM_PLATFORM_REGISTRY[platform_name]
        capability.validate_config(f"[{section}]", platform_config)
        sites[section] = VMSiteDecl(
            name=section,
            platform=platform_name,
            platform_config=platform_config,
            declared_at=decls.lookup(section),
        )
    return sites


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
) -> tuple[str, ...]:
    """Warn ``[secret_backends.*]`` sections as deprecated no-ops.

    The backend-keyed TOML sections never carried configuration (only
    the backend name itself), and the backends are registered code
    capabilities -- so a section here is semantically empty. Known
    backends warn as deprecated; unknown ones (typo ``envvar`` for
    ``env-var``) stay a hard ``ConfigError`` for typo protection.
    Nothing is stored and nothing publishes.

    Returns the display shapes of the sections found (facts for
    surfaces with their own rendering, mirroring
    ``_warn_deprecated_resource_sections``).
    """
    raw = data.get("secret_backends", {})
    if not isinstance(raw, dict):
        raise ConfigError("[secret_backends] must be a table")

    from agentworks.secrets.backends import SECRET_BACKEND_REGISTRY

    known_backends = set(SECRET_BACKEND_REGISTRY)
    found: list[str] = []
    for key, bdata in raw.items():
        backend_str = str(key)
        if not isinstance(bdata, dict):
            raise ConfigError(f"secret_backends.{backend_str} must be a table")
        if backend_str not in known_backends:
            raise ConfigError(
                f"[secret_backends.{backend_str}] names an unknown secret "
                f"backend; supported: {sorted(known_backends)}"
            )
        found.append(f"[secret_backends.{backend_str}]")
        deprecations.append(
            f"[secret_backends.{backend_str}] is deprecated and has no effect: "
            f"the built-in backends ship with agentworks, and activation is "
            f"[secret_config].backends. Remove the section, or run "
            f"`agw resource migrate --all` to drop it."
        )
    return tuple(found)


def _warn_deprecated_resource_sections(
    data: dict[str, object],
    deprecations: list[str],
) -> tuple[str, ...]:
    """ONE aggregated deprecation issue for the TOML resource sections
    present (Phase 5, aggregated at maintainer direction -- a warning
    per section was obnoxious on real configs).

    Dual-path is permanent policy short of a future major release: these
    sections keep loading with exactly today's semantics. The warning is
    the nudge toward the YAML manifest surface. ``[secret_backends.*]``
    is excluded -- it has its own no-op message above -- and
    ``[secret_config]`` is config, not a resource section.

    Returns the display shapes of the sections found, so surfaces with
    their own rendering (doctor's tidy one-line row) can compose from
    the fact instead of reusing this ambient teaching text.
    """
    from agentworks.manifests.decode import KIND_SECTIONS

    present: list[str] = []
    for _kind, sections in KIND_SECTIONS.items():
        for section in sections:
            if section == "secret_backends" or section not in data:
                continue
            # Display the header shape operators can actually grep for:
            # [admin.config], [named_console], and the legacy vm-site
            # sections ([azure] / [proxmox]) are non-family sections;
            # everything else nests names ([secrets.<name>]).
            if section == "admin":
                present.append("[admin.config]")
            elif section in ("named_console", "azure", "proxmox"):
                present.append(f"[{section}]")
            else:
                present.append(f"[{section}.*]")
    if not present:
        return ()
    noun = "section" if len(present) == 1 else "sections"
    # Selectors are KIND names, not section names: [azure]/[proxmox]
    # migrate as `vm-site`, which nothing on screen would suggest.
    site_hint = (
        " (the [azure]/[proxmox] sections migrate as `vm-site`)"
        if any(s in ("[azure]", "[proxmox]") for s in present)
        else ""
    )
    deprecations.append(
        f"deprecated TOML resource {noun}: {', '.join(present)}. Move "
        f"these with `agw resource migrate <kind>` or `--all`{site_hint}, "
        f"declare new resources as YAML manifests "
        f"(`agw resource sample <kind>`), or silence this warning with "
        f"--no-deprecations. TOML resource support will likely be removed "
        f"in a future major release."
    )
    return tuple(present)


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


def load_config(
    path: Path | None = None,
    *,
    warn_issues: bool = True,
    warn_deprecations: bool = True,
    resources: bool = True,
) -> Config:
    """Load and validate the agentworks configuration.

    Args:
        path: Override config file path (default: ~/.config/agentworks/config.toml).
        warn_issues: Emit config issues as warnings to stderr (default: True).
            Set to False when the caller handles issues itself (e.g. doctor).
        warn_deprecations: Emit the TOML-resource deprecation nudge (default:
            True; also silenceable per-invocation via --no-deprecations). Set
            to False for commands that ARE the remediation the nudge points at
            (e.g. ``agw resource migrate``) -- nagging them is noise.
        resources: Load the TOML resource sections into Config (default:
            True). Settings-only callers (e.g. ``agw resource sample --write``,
            which only needs ``source_path``) pass False: resource sections are
            neither validated nor deprecation-warned, and the resulting Config
            carries ``resources_loaded=False`` -- ``build_registry`` refuses it,
            so a settings-only Config can never silently publish an empty TOML
            side into a registry.

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

    # Settings-only mode: resource loaders see an empty document, so they
    # produce their framework defaults with zero issues or deprecations.
    # Settings loaders (operator, paths, defaults, session, secret_config)
    # always see the real data; they are config. The legacy [azure] /
    # [proxmox] sections are RESOURCE declarations (vm-site rows) and go
    # through resource_data like every other resource section.
    resource_data = data if resources else {}

    git_credentials = _load_git_credentials(resource_data, issues, decls)
    apt_sources, apt_packages, system_cmds, user_cmds = _load_catalog_sections(resource_data)

    session_config = _load_session_config(data, issues)
    session_templates = _load_session_templates(resource_data, issues, decls)

    loaded_vm_templates = _load_vm_templates(resource_data, issues, decls)
    loaded_agent_templates = _load_agent_templates(resource_data, issues, decls)


    admin = _load_admin_config(resource_data, issues, decls)
    workspace_templates = _load_workspace_templates(resource_data, issues, decls)

    secrets = _load_secrets(resource_data, issues, decls)
    deprecations: list[str] = []
    noop_backend_sections = _load_secret_backends(resource_data, deprecations)
    deprecated_sections = _warn_deprecated_resource_sections(
        resource_data, deprecations
    )
    secret_config_data = _load_secret_config(data, issues, decls)
    # Phase 1b: env-block secret references no longer error at config load
    # when they don't match a [secrets.<name>] block; the framework
    # auto-declares them at finalize. Resolution runs through the active
    # backends (``agentworks.secrets.resolve``).

    config = Config(
        operator=_load_operator(data, issues),
        paths=_load_paths(data),
        defaults=_load_defaults(data, issues, deprecations),
        named_console=_load_named_console(resource_data, issues, decls),
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
        vm_sites=_load_vm_sites_legacy(resource_data, decls),
        secrets=secrets,
        secret_config_data=secret_config_data,
        config_issues=tuple(issues),
        deprecation_issues=tuple(deprecations),
        deprecated_sections=deprecated_sections,
        noop_secret_backend_sections=noop_backend_sections,
        resources_loaded=resources,
    )

    if warn_issues and config.config_issues:
        from agentworks.output import warn

        for issue in config.config_issues:
            warn(f"Config: {issue}")
    if warn_issues and warn_deprecations and config.deprecation_issues:
        from agentworks.output import deprecations_suppressed, warn

        if not deprecations_suppressed():
            for issue in config.deprecation_issues:
                warn(f"Config: {issue}")

    return config
