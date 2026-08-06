"""The migrator's private TOML resource reader (the pre-side oracle).

config.toml no longer declares resources (ADR 0022; it is settings only),
so these loaders no longer run on the config-load path. They are RELOCATED
here, unchanged, to serve one job: read the ORIGINAL config file text and
reconstruct the resource decls the operator's TOML declared, keyed by
``(kind, name)``. ``agw resource migrate`` compares this independent
derivation against the registry rebuilt from the emitted YAML manifests
(the post-side), so the comparison is a real test of the emission mapping
rather than a tautology: the oracle reads the FLAT TOML shape, the
post-side reads the emitted TAGGED YAML.

These loaders were split out of ``agentworks.config`` (``loaders_resources``
in full, ``loaders_sessions``'s ``_load_session_templates``,
``loaders_secrets``'s ``_load_secrets``, and ``loaders_core``'s
``_load_git_credentials``). They still import the shared leaf machinery
(``_warn_unexpected_keys``, ``_raise_unexpected_keys``, ``_parse_env_table``,
the two nonconforming-secret-name helpers, ``validate_name``) from ``config``,
so the fork with the manifest decoders (which own their per-kind validation
now) shares its measuring stick and stays narrow.
"""

from __future__ import annotations

import tomllib
from typing import TYPE_CHECKING, Any, Literal, cast

from agentworks.agents.template import AgentTemplate
from agentworks.config.loaders_core import (
    _parse_env_table,
    _raise_unexpected_keys,
    _require_string_list,
    _warn_nonconforming_derived_secret,
    _warn_nonconforming_secret_name,
    _warn_unexpected_keys,
)
from agentworks.config.models import _SectionLineMap
from agentworks.config.validation import MAX_SECRET_NAME_LENGTH, validate_name
from agentworks.errors import ConfigError
from agentworks.git_credentials.credential import GitCredentialConfig
from agentworks.secrets import SecretDecl
from agentworks.sessions.layouts import AW_SESSION_VERTICAL_LAYOUT, VALID_TMUX_LAYOUTS, TmuxLayout
from agentworks.sessions.template import NamedConsoleConfig, SessionTemplate
from agentworks.source_location import scan_section_lines
from agentworks.vms.admin import AdminConfig
from agentworks.vms.template import VMTemplate
from agentworks.workspaces.template import WorkspaceTemplate

if TYPE_CHECKING:
    from pathlib import Path

    from agentworks.env import EnvEntry
    from agentworks.vms.sites import VMSiteDecl

_NAMED_CONSOLE_KEYS = {"description", "tmux_layout"}


def _load_named_console(
    data: dict[str, object],
    issues: list[str],
    decls: _SectionLineMap,
) -> NamedConsoleConfig | None:
    if "named_console" not in data:
        return None
    raw = data.get("named_console", {})
    if not isinstance(raw, dict):
        raise ConfigError("[named_console] must be a table")

    _warn_unexpected_keys(raw, _NAMED_CONSOLE_KEYS, "named_console", issues)

    layout = raw.get("tmux_layout", AW_SESSION_VERTICAL_LAYOUT)
    if layout not in VALID_TMUX_LAYOUTS:
        raise ConfigError(f"named_console.tmux_layout must be one of {VALID_TMUX_LAYOUTS}, got: {layout}")

    return NamedConsoleConfig(
        name="default",
        # The membership check above is what proves the cast; the oracle
        # validates by tuple because it is written independently of the row's
        # own model, which is the whole point of it (descriptor LLD 11).
        tmux_layout=cast("TmuxLayout", layout),
        description=str(raw["description"]) if "description" in raw else None,
        declared_at=decls.lookup("named_console"),
    )


_VM_TEMPLATE_KEYS = {
    "inherits",
    "description",
    "cpus",
    "memory",
    "disk",
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
        raise ConfigError("[vm.config] has been replaced by [vm_templates.default].")

    templates: dict[str, VMTemplate] = {}
    for name, tdata in raw.items():
        if not isinstance(tdata, dict):
            raise ConfigError(f"vm_templates.{name} must be a table")
        _warn_unexpected_keys(tdata, _VM_TEMPLATE_KEYS, f"vm_templates.{name}", issues)

        # tailscale_auth_key must be a non-empty bare string secret name;
        # absence means "inherit" and the resolver applies the default
        # ``tailscale-auth-key``. Empty-string is rejected (it would derive
        # a ``AW_SECRET_`` env-var name and prompt for a secret named "").
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
                    f'"tailscale-auth-key"'
                )
            ts_key_raw = tdata["tailscale_auth_key"]
            _warn_nonconforming_secret_name(
                ts_key_raw, location=f"vm_templates.{name}.tailscale_auth_key", issues=issues
            )

        templates[name] = VMTemplate(
            name=name,
            inherits=list(tdata.get("inherits", [])),
            description=str(tdata["description"]) if "description" in tdata else None,
            cpus=int(tdata["cpus"]) if "cpus" in tdata else None,
            memory=int(tdata["memory"]) if "memory" in tdata else None,
            disk=int(tdata["disk"]) if "disk" in tdata else None,
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

    return templates


_USER_CONFIG_KEYS = {
    "description",
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
    name: str = "default",
) -> AdminConfig | None:
    """Load admin per-user config from [admin.config]."""
    if "admin" not in data:
        return None
    top = data.get("admin", {})
    if not isinstance(top, dict):
        raise ConfigError("[admin] must be a table")
    raw = top.get("config", {})
    if not isinstance(raw, dict):
        raise ConfigError("[admin.config] must be a table")

    _warn_unexpected_keys(raw, _USER_CONFIG_KEYS, "admin.config", issues)

    packages = _require_string_list(raw, "mise_packages", "admin.config")
    lockfile_value = raw.get("mise_lockfile")
    if lockfile_value is not None and not isinstance(lockfile_value, str):
        raise ConfigError("admin.config.mise_lockfile must be a string")
    lockfile = lockfile_value
    install_before_value = raw.get("mise_install_before", "7d")
    if not isinstance(install_before_value, str):
        raise ConfigError("admin.config.mise_install_before must be a string")
    install_before = install_before_value
    from agentworks.config.validation import validate_mise_settings

    validate_mise_settings(packages, lockfile, install_before, context="admin.config")

    return AdminConfig(
        name=name,
        description=str(raw["description"]) if "description" in raw else None,
        username=str(raw.get("username", "agentworks")),
        shell=str(raw.get("shell", "bash")),
        git_credentials=list(raw.get("git_credentials", [])),
        user_install_commands=list(raw.get("user_install_commands", [])),
        dotfiles_source=str(raw["dotfiles_source"]) if "dotfiles_source" in raw else None,
        dotfiles_destination=str(raw.get("dotfiles_destination", "~/.dotfiles")),
        dotfiles_install_cmd=str(raw.get("dotfiles_install_cmd", "./install.sh")),
        mise_activate=bool(raw.get("mise_activate", True)),
        mise_packages=packages,
        mise_lockfile=lockfile,
        mise_allow_unlocked=bool(raw.get("mise_allow_unlocked", False)),
        mise_install_before=install_before,
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
        raise ConfigError("[agent.config] has been replaced by [agent_templates.default].")

    templates: dict[str, AgentTemplate] = {}
    for name, tdata in raw.items():
        if not isinstance(tdata, dict):
            raise ConfigError(f"agent_templates.{name} must be a table")
        _warn_unexpected_keys(tdata, _AGENT_TEMPLATE_KEYS, f"agent_templates.{name}", issues)

        packages = _require_string_list(tdata, "mise_packages", f"agent_templates.{name}")
        lockfile_value = tdata.get("mise_lockfile")
        if lockfile_value is not None and not isinstance(lockfile_value, str):
            raise ConfigError(f"agent_templates.{name}.mise_lockfile must be a string")
        lockfile = lockfile_value
        install_before_value = tdata.get("mise_install_before", "7d")
        if not isinstance(install_before_value, str):
            raise ConfigError(f"agent_templates.{name}.mise_install_before must be a string")
        install_before = install_before_value
        from agentworks.config.validation import validate_mise_settings

        validate_mise_settings(packages, lockfile, install_before, context=f"agent_templates.{name}")

        templates[name] = AgentTemplate(
            name=name,
            inherits=list(tdata.get("inherits", [])),
            description=str(tdata["description"]) if "description" in tdata else None,
            shell=str(tdata["shell"]) if "shell" in tdata else None,
            git_credentials=list(tdata["git_credentials"]) if "git_credentials" in tdata else None,
            user_install_commands=(list(tdata["user_install_commands"]) if "user_install_commands" in tdata else None),
            dotfiles_source=str(tdata["dotfiles_source"]) if "dotfiles_source" in tdata else None,
            dotfiles_destination=(str(tdata["dotfiles_destination"]) if "dotfiles_destination" in tdata else None),
            dotfiles_install_cmd=(str(tdata["dotfiles_install_cmd"]) if "dotfiles_install_cmd" in tdata else None),
            mise_activate=bool(tdata["mise_activate"]) if "mise_activate" in tdata else None,
            mise_packages=packages if "mise_packages" in tdata else None,
            mise_lockfile=lockfile,
            mise_allow_unlocked=(bool(tdata["mise_allow_unlocked"]) if "mise_allow_unlocked" in tdata else None),
            mise_install_before=(install_before if "mise_install_before" in tdata else None),
            mise_prune_on_reinit=(bool(tdata["mise_prune_on_reinit"]) if "mise_prune_on_reinit" in tdata else None),
            claude_marketplaces=(
                _require_string_list(tdata, "claude_marketplaces", f"agent_templates.{name}")
                if "claude_marketplaces" in tdata
                else None
            ),
            claude_plugins=(
                _require_string_list(tdata, "claude_plugins", f"agent_templates.{name}")
                if "claude_plugins" in tdata
                else None
            ),
            env=_parse_env_table(tdata.get("env"), context=f"agent_templates.{name}", issues=issues),
            declared_at=decls.lookup("agent_templates", name),
        )

    return templates


def _load_apt_and_install_sections(
    data: dict[str, object],
) -> tuple[
    dict[str, object],
    dict[str, object],
    dict[str, object],
    dict[str, object],
]:
    """Load the four user-defined apt / install-command sections as raw dicts.

    Actual parsing into typed entries happens in the ``apt`` and
    ``install_commands`` operator publishers. Here we just validate that
    each section is a table of tables.
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


_WORKSPACE_TEMPLATE_KEYS = {
    "inherits",
    "description",
    "repo",
    "tmuxinator",
    "git_user_name",
    "git_user_email",
    "env",
}


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
        _warn_unexpected_keys(tdata, _WORKSPACE_TEMPLATE_KEYS, f"workspace_templates.{name}", issues)
        repo = str(tdata["repo"]) if "repo" in tdata else None
        templates[name] = WorkspaceTemplate(
            name=name,
            inherits=list(tdata.get("inherits", [])),
            description=str(tdata["description"]) if "description" in tdata else None,
            repo=repo,
            tmuxinator=bool(tdata["tmuxinator"]) if "tmuxinator" in tdata else None,
            git_user_name=(str(tdata["git_user_name"]) if "git_user_name" in tdata else None),
            git_user_email=(str(tdata["git_user_email"]) if "git_user_email" in tdata else None),
            env=_parse_env_table(
                tdata.get("env"),
                context=f"workspace_templates.{name}",
                issues=issues,
            ),
            declared_at=decls.lookup("workspace_templates", name),
        )

    return templates


# The flat legacy [azure] / [proxmox] keys that hoist into the nested
# platform_config blob. The flat domain stays silently loose on stray keys
# (the git-credential ``org`` precedent); manifests validate the true blob
# strictly.
_LEGACY_SITE_SECTIONS: dict[str, tuple[str, tuple[str, ...]]] = {
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
    issues: list[str],
    decls: _SectionLineMap,
) -> dict[str, VMSiteDecl]:
    """Load the legacy ``[azure]`` / ``[proxmox]`` sections as ``vm-site``
    resources.

    Flat TOML is the one place platform-owned fields sit outside the
    ``platform_config`` blob; this loader nests at the boundary (section
    name becomes the site name, ``platform`` is synthesized).
    """
    from agentworks.vms.sites import VMSiteDecl

    sites: dict[str, VMSiteDecl] = {}
    for section, (platform_name, known_keys) in _LEGACY_SITE_SECTIONS.items():
        raw = data.get(section)
        if raw is None:
            continue
        if not isinstance(raw, dict):
            raise ConfigError(f"[{section}] must be a table")
        platform_config: dict[str, object] = {key: raw[key] for key in known_keys if key in raw}
        token_secret = platform_config.get("token_secret")
        if isinstance(token_secret, str) and token_secret:
            _warn_nonconforming_secret_name(token_secret, location=f"{section}.token_secret", issues=issues)
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
        validate_name(name_str, max_length=MAX_SECRET_NAME_LENGTH)
        _warn_unexpected_keys(sdata, expected, f"secrets.{name_str}", issues)

        description = sdata.get("description")
        if not isinstance(description, str) or not description:
            raise ConfigError(f"secrets.{name_str}.description is required and must be a non-empty string")
        hint = sdata.get("hint")
        if hint is not None and not isinstance(hint, str):
            raise ConfigError(f"secrets.{name_str}.hint must be a string")

        raw_mappings = sdata.get("backend_mappings", {})
        if not isinstance(raw_mappings, dict):
            raise ConfigError(f"secrets.{name_str}.backend_mappings must be a table")
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
                    f"secrets.{name_str}.backend_mappings.{kind_str}: must be a string, inline table, or false"
                )

        secret_decls[name_str] = SecretDecl(
            name=name_str,
            description=description,
            hint=hint,
            backend_mappings=backend_mappings,
            declared_at=decls.lookup("secrets", name_str),
        )
    return secret_decls


# The legacy flat fields (``shell``'s config vocabulary) plus the canonical
# harness-integration pair. The flat fields keep
# loading verbatim; the loader hoists them into the canonical
# ``harness_integration = "shell"`` plus ``harness_integration_config`` shape.
_SESSION_TEMPLATE_KEYS = {
    "inherits",
    "description",
    "harness_integration",
    "harness_integration_config",
    "command",
    "resume_command",
    "required_commands",
    "env",
}
_SHELL_FLAT_FIELDS = ("command", "resume_command", "required_commands")


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
        _raise_unexpected_keys(tdata, _SESSION_TEMPLATE_KEYS, f"session_templates.{name}")
        env: dict[str, EnvEntry] | None = None
        if "env" in tdata:
            env = _parse_env_table(
                tdata["env"],
                context=f"session_templates.{name}",
                issues=issues,
            )
        harness_integration, harness_integration_config = _session_harness_integration_pair(name, tdata)
        templates[name] = SessionTemplate(
            name=name,
            inherits=list(tdata.get("inherits", [])),
            description=str(tdata["description"]) if "description" in tdata else None,
            harness_integration=harness_integration,
            harness_integration_config=harness_integration_config,
            env=env,
            declared_at=decls.lookup("session_templates", name),
        )

    return templates


def _session_harness_integration_pair(
    name: str, tdata: dict[str, object]
) -> tuple[str | None, dict[str, object] | None]:
    """Resolve a TOML session-template's harness-integration selector/config pair.

    Legacy flat fields are hoisted onto the ``shell`` harness integration.
    ``None`` on either result means "not declared here".
    """
    selector = "harness_integration"
    config_selector = "harness_integration_config"
    flat_present = [key for key in _SHELL_FLAT_FIELDS if key in tdata]
    harness_integration_val = tdata.get(selector)
    if harness_integration_val is not None and not isinstance(harness_integration_val, str):
        raise ConfigError(f"session_templates.{name}.{selector} must be a string")

    if flat_present:
        if harness_integration_val is not None and harness_integration_val != "shell":
            raise ConfigError(
                f"session_templates.{name}: the legacy field(s) "
                f"{', '.join(flat_present)} configure the 'shell' harness integration "
                f"and cannot combine with {selector} = {harness_integration_val!r}; put "
                f"the workload under [session_templates.{name}.{config_selector}]"
            )
        if config_selector in tdata:
            raise ConfigError(
                f"session_templates.{name}: the legacy field(s) "
                f"{', '.join(flat_present)} cannot combine with an explicit "
                f"{config_selector} table (one spelling per declaration); put "
                f"the commands under {config_selector} instead"
            )
        blob: dict[str, object] = {}
        if "command" in tdata:
            blob["command"] = str(tdata["command"])
        if "resume_command" in tdata:
            blob["resume_command"] = str(tdata["resume_command"])
        if "required_commands" in tdata:
            blob["required_commands"] = _require_string_list(tdata, "required_commands", f"session_templates.{name}")
        harness_integration: str | None = "shell"
        harness_integration_config: dict[str, object] | None = blob
    else:
        harness_integration = harness_integration_val
        harness_integration_config = None
        if config_selector in tdata:
            raw_config = tdata[config_selector]
            if not isinstance(raw_config, dict):
                raise ConfigError(f"session_templates.{name}.{config_selector} must be a table")
            harness_integration_config = dict(raw_config)
        if harness_integration is None and harness_integration_config is not None:
            raise ConfigError(
                f"session_templates.{name}: {config_selector} needs a selector "
                f'(a blob with no owner); add {selector} = "..."'
            )

    return harness_integration, harness_integration_config


def _load_git_credentials(
    data: dict[str, object],
    issues: list[str],
    decls: _SectionLineMap,
    *,
    warn_ignored_scope_keys: bool = True,
) -> dict[str, GitCredentialConfig]:
    """The flat ``[git_credentials.*]`` reader.

    Relocated here with the other resource loaders (ADR 0022). The two
    nonconforming-secret-name helpers it calls stay shared in
    ``config.loaders_core``, so the git-credential manifest decoder (which
    owns its per-kind validation after the fork) can call the same derived
    -secret helper directly and keep warning parity with this oracle.
    """
    raw = data.get("git_credentials", {})
    if not isinstance(raw, dict):
        raise ConfigError("[git_credentials] must be a table")

    creds: dict[str, GitCredentialConfig] = {}
    for name, cdata in raw.items():
        if not isinstance(cdata, dict):
            raise ConfigError(f"git_credentials.{name} must be a table")
        # ``provider`` is the vocabulary going forward (matching secret-
        # backend manifests); ``type`` remains accepted for the flat TOML
        # shape. ``provider`` wins when both are present.
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

        provider_config: dict[str, object] = {}
        # ``token`` is a bare secret name the provider sources its PAT from.
        # Flat in TOML, hoisted into provider_config so the internal rep
        # matches the YAML manifest shape. Empty-string is rejected.
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
                    f'"git-token-{name}"'
                )
            _warn_nonconforming_secret_name(cdata["token"], location=f"git_credentials.{name}.token", issues=issues)
            provider_config["token"] = cdata["token"]
        else:
            _warn_nonconforming_derived_secret(name, issues)
        # The flat TOML shape only ever read ``org`` (azdo). github scope
        # keys warn (silence would ship a broader-authority credential than
        # declared); other stray keys stay silently ignored.
        if warn_ignored_scope_keys and cred_type == "github":
            ignored_scopes = sorted({"repo", "repos", "owner"} & set(cdata))
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
        creds[name] = GitCredentialConfig(
            name=name,
            provider=cred_type,
            provider_config=provider_config,
            description=str(cdata["description"]) if "description" in cdata else None,
            declared_at=decls.lookup("git_credentials", name),
        )
    return creds


def toml_resource_rows(config_path: Path) -> dict[tuple[str, str], Any]:
    """The migrator's independent pre-side oracle.

    Read the ORIGINAL config file text and reconstruct every TOML-declared
    resource decl, keyed by ``(kind, name)``. Soft issues (unknown-key
    warnings, nonconforming-secret-name nudges) are collected but discarded:
    the oracle exists to produce the decls the registry comparison uses, not
    to surface config-load warnings (config.toml declaring resources is a
    hard error on the normal load path now).

    ``declared_at`` and the other source-dependent fields are left on the
    decls; the caller (``plan_migration``) normalizes them through
    ``strip_source_fields`` before comparing.
    """
    from agentworks.apt import _load_apt_packages, _load_apt_sources
    from agentworks.install_commands import _load_system_commands, _load_user_commands

    raw_text = config_path.read_text()
    data = tomllib.loads(raw_text)
    decls = _SectionLineMap(config_path=config_path, section_lines=scan_section_lines(raw_text))
    issues: list[str] = []

    rows: dict[tuple[str, str], Any] = {}

    for name, secret in _load_secrets(data, issues, decls).items():
        rows[("secret", name)] = secret
    for name, vm_template in _load_vm_templates(data, issues, decls).items():
        rows[("vm-template", name)] = vm_template
    for name, agent_template in _load_agent_templates(data, issues, decls).items():
        rows[("agent-template", name)] = agent_template
    for name, workspace_template in _load_workspace_templates(data, issues, decls).items():
        rows[("workspace-template", name)] = workspace_template
    for name, session_template in _load_session_templates(data, issues, decls).items():
        rows[("session-template", name)] = session_template
    for name, credential in _load_git_credentials(data, issues, decls).items():
        rows[("git-credential", name)] = credential
    for name, site in _load_vm_sites_legacy(data, issues, decls).items():
        rows[("vm-site", name)] = site

    admin = _load_admin_config(data, issues, decls)
    if admin is not None:
        rows[("admin-template", admin.name)] = admin
    named_console = _load_named_console(data, issues, decls)
    if named_console is not None:
        rows[("named-console-template", "default")] = named_console

    apt_sources, apt_packages, system_cmds, user_cmds = _load_apt_and_install_sections(data)
    for name, apt_source in _load_apt_sources(apt_sources, decls).items():
        rows[("apt-source", name)] = apt_source
    for name, apt_package in _load_apt_packages(apt_packages, decls).items():
        rows[("apt-package", name)] = apt_package
    for name, system_cmd in _load_system_commands(system_cmds, decls).items():
        rows[("system-install-command", name)] = system_cmd
    for name, user_cmd in _load_user_commands(user_cmds, decls).items():
        rows[("user-install-command", name)] = user_cmd

    return rows
