"""Generic TOML-loading helpers plus the settings-section loaders
(``[operator]`` / ``[paths]`` / ``[defaults]``).

The resource loaders relocated to ``agentworks.migrate.toml_resources``
when config.toml stopped declaring resources (ADR 0022). ``_parse_env_table``
and the two unknown-key helpers stayed behind and are shared from here;
the settings sections still use the unknown-key ones, and the migrator's
frozen TOML oracle uses all three.

The two nonconforming-secret-name helpers that used to live here are gone.
The manifest side derives that warning structurally from the ``SecretRef``
edges the models declare (``manifests.decode.advisory_issues``, which is
what issue #311 asked for), and the oracle's copy warned into a list it
discards.

Split out of the former monolithic ``agentworks/config.py`` (see
``agentworks/config/__init__.py`` for the package overview).
"""

from __future__ import annotations

import ipaddress
import re
from pathlib import Path

from agentworks.config.models import DefaultsConfig, OperatorConfig, PathsConfig
from agentworks.config.validation import validate_vm_workspaces
from agentworks.env import EnvEntry
from agentworks.errors import ConfigError
from agentworks.naming import SSH_HOST_PREFIX_RE


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

    **The declarative schema did not retire this, against planning text
    that says it did.** FR12 flipped an unknown key in a KIND's spec from
    a soft issue to a hard pydantic error, and the kind callers went with
    the decoders they lived in. Every caller left was always something
    else:

    - three settings sections ([operator], [secret_config],
      [session.config]), where the soft convention is the deliberate one
      (doctor wants every issue in the file, not the first). [plugins]
      departs from it on purpose and says why at ``_load_plugins``;
    - the migrator's TOML oracle (``migrate.toml_resources``), which
      reads the ORIGINAL flat shape through the loaders as they were and
      then discards the issues it collects (``toml_resource_rows``).

    So this expires when the settings sections themselves become models
    (FR14), which is its own effort, not with the last kind decoder.
    """
    unexpected = set(raw.keys()) - known
    if unexpected:
        keys = ", ".join(sorted(unexpected))
        issues.append(f"unexpected keys in [{section}]: {keys}")


def _raise_unexpected_keys(raw: dict[str, object], known: set[str], section: str) -> None:
    """Reject unexpected keys in a strict settings section."""
    unexpected = set(raw) - known
    if unexpected:
        keys = ", ".join(sorted(unexpected))
        raise ConfigError(f"unexpected keys in [{section}]: {keys}")


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
            result[key_str] = EnvEntry(value=val)
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
            result[key_str] = EnvEntry(secret=secret_name)
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
    "ssh_allow_cidrs",
}

_SSH_KEY_HINT = "Point it at a key you already use, or create one with `ssh-keygen -t ed25519`."
"""Remedy for the missing-key errors below.

The sample config ships a plausible default path for both keys, so on a
machine with no key there this is the first error a greenfield operator
meets, and `agw config init` sends them straight at it. The message was
correct and specific and said nothing about what to do about it.
"""


def _load_operator(data: dict[str, object], issues: list[str]) -> OperatorConfig:
    raw = data.get("operator")
    if not isinstance(raw, dict):
        raise ConfigError("[operator] section is required")

    _warn_unexpected_keys(raw, _OPERATOR_KEYS, "operator", issues)

    pub = _expand(str(_require(raw, "ssh_public_key", "operator")))
    priv = _expand(str(_require(raw, "ssh_private_key", "operator")))

    if not pub.exists():
        raise ConfigError(f"operator.ssh_public_key does not exist: {pub}", hint=_SSH_KEY_HINT)
    if not priv.exists():
        raise ConfigError(f"operator.ssh_private_key does not exist: {priv}", hint=_SSH_KEY_HINT)

    ssh_config = Path.home() / ".ssh" / "config"
    if "ssh_config" in raw:
        ssh_config = _expand(str(raw["ssh_config"]))

    extra_keys: list[Path] = []
    for entry in raw.get("extra_ssh_public_keys", []):
        p = _expand(str(entry))
        if not p.exists():
            raise ConfigError(f"operator.extra_ssh_public_keys: file does not exist: {p}")
        extra_keys.append(p)

    # Extra sources allowed through the transient cloud SSH firewall
    # hole; validated here so a typo fails at config load, not at the
    # first vm op that pokes the hole. Stored normalized (a bare IP
    # becomes its /32) so downstream consumers compare and poke
    # canonical prefixes. The list guard keeps a scalar (a bare string
    # would otherwise iterate per character) a typed error too.
    raw_cidrs = raw.get("ssh_allow_cidrs", [])
    if not isinstance(raw_cidrs, list):
        raise ConfigError("operator.ssh_allow_cidrs must be a list of IPv4 addresses and/or CIDRs")
    allow_cidrs: list[str] = []
    for entry in raw_cidrs:
        text = str(entry).strip()
        try:
            allow_cidrs.append(str(ipaddress.IPv4Network(text, strict=False)))
        except ValueError as exc:
            raise ConfigError(
                f"operator.ssh_allow_cidrs: invalid entry {text!r}: must be an IPv4 address or CIDR"
            ) from exc

    host_prefix = str(raw.get("ssh_host_prefix", "awvm--"))
    if not SSH_HOST_PREFIX_RE.match(host_prefix):
        raise ConfigError(
            f"operator.ssh_host_prefix must be alphanumeric with hyphens, underscores, "
            f"or dots (no whitespace or special characters), got: {host_prefix!r}"
        )

    agent_host_prefix = str(raw.get("ssh_agent_host_prefix", "awagent--"))
    if not SSH_HOST_PREFIX_RE.match(agent_host_prefix):
        raise ConfigError(
            f"operator.ssh_agent_host_prefix must be alphanumeric with hyphens, underscores, "
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
        ssh_allow_cidrs=allow_cidrs,
    )


def _load_paths(data: dict[str, object]) -> PathsConfig:
    raw = data.get("paths", {})
    if not isinstance(raw, dict):
        raise ConfigError("[paths] must be a table")
    _raise_unexpected_keys(raw, {"vm_workspaces", "vscode_workspaces", "backups"}, "paths")
    defaults = PathsConfig()
    vm_ws = str(raw["vm_workspaces"]) if "vm_workspaces" in raw else defaults.vm_workspaces
    validate_vm_workspaces(vm_ws)
    vscode_ws = _expand(str(raw["vscode_workspaces"])) if "vscode_workspaces" in raw else defaults.vscode_workspaces
    backups = _expand(str(raw["backups"])) if "backups" in raw else defaults.backups
    return PathsConfig(vm_workspaces=vm_ws, vscode_workspaces=vscode_ws, backups=backups)


_DEFAULTS_KEYS = {"site", "runup_git_credentials"}


def _load_defaults(data: dict[str, object]) -> DefaultsConfig:
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

    _raise_unexpected_keys(raw, _DEFAULTS_KEYS, "defaults")

    # `site` names a vm-site resource; existence is validated at the
    # composition boundary (vms.validate_sites), where the finalized
    # registry knows every declared site.
    site = raw.get("site")
    if site is not None and (not isinstance(site, str) or not site):
        raise ConfigError("defaults.site must be a non-empty site name")
    return DefaultsConfig(
        site=str(site) if site is not None else None,
        runup_git_credentials=bool(raw.get("runup_git_credentials", True)),
    )
