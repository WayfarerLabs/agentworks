"""Generic TOML-loading helpers, plus the settings-section loaders
(``[operator]`` / ``[paths]`` / ``[defaults]``) and the ``[git_credentials]``
resource loader.

Split out of the former monolithic ``agentworks/config.py`` (see
``agentworks/config/__init__.py`` for the package overview).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from agentworks.config.models import DefaultsConfig, OperatorConfig, PathsConfig, _SectionLineMap
from agentworks.config.validation import SSH_HOST_PREFIX_RE
from agentworks.env import EnvEntry
from agentworks.errors import ConfigError
from agentworks.git_credentials.credential import GitCredentialConfig


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
                f"{context}.env: invalid env var name {key_str!r} (must match /^[A-Za-z_][A-Za-z0-9_]*$/)"
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
                'inline table of the form { secret = "<name>" }'
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
            "defaults.git_credentials has been removed. Move git_credentials into [admin.config] and/or [agent.config]."
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
            "defaults.vm_host has been removed; remote Lima hosts are vm-site resources now",
            hint=(
                site_manifest_hint(old_name, vm_host="<user@host>") + "\n\nthen set defaults.site to the site's name"
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
        # The ``type`` field's reference-existence check
        # lives in the framework via
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
                    f'"git-token-{name}"'
                )
            provider_config["token"] = cdata["token"]
        # The flat TOML shape only ever read ``org``, and only for azdo;
        # hoisting it into the blob for other providers would promote a
        # historically-ignored stray key into a validation error and
        # break released configs (loads-today). The flat domain's
        # stray-key silence stays until the flat shape is retired, EXCEPT
        # github scope keys, where silence would ship a credential with
        # BROADER authority than the operator declared; those warn.
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
