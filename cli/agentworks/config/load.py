"""Top-level ``load_config`` entry point: reads the TOML file, pre-scans it
for section-header line numbers, and drives every settings-section loader
to compose a validated ``Config``.

config.toml is settings only now (ADR 0022). Resource-declaring sections
are ordinary unexpected top-level keys.

Split out of the former monolithic ``agentworks/config.py`` (see
``agentworks/config/__init__.py`` for the package overview).
"""

from __future__ import annotations

import sys
import tomllib
from typing import TYPE_CHECKING

from agentworks.config.loaders_core import _load_defaults, _load_operator, _load_paths
from agentworks.config.loaders_database import _load_database_config
from agentworks.config.loaders_secrets import _load_plugins, _load_secret_config
from agentworks.config.loaders_sessions import _load_session_config
from agentworks.config.models import Config, _SectionLineMap
from agentworks.errors import ConfigError, ConfigFileNotFoundError
from agentworks.path_rendering import format_host_path
from agentworks.source_location import scan_section_lines

if TYPE_CHECKING:
    from pathlib import Path

EXPECTED_TOP_LEVEL_KEYS = {
    "operator",
    "paths",
    "defaults",
    "database",
    "session",
    "secret_config",
    "plugins",
}


def _raise_unexpected_top_level_keys(data: dict[str, object]) -> None:
    """Reject unexpected top-level keys.

    This catches a common TOML pitfall: uncommenting a key without its section
    header causes the key to land in the wrong (or top-level) section.
    """
    unexpected = sorted(set(data.keys()) - EXPECTED_TOP_LEVEL_KEYS)
    if not unexpected:
        return
    raise ConfigError(f"unexpected top-level keys in config: {', '.join(unexpected)}")


def load_config(
    path: Path | None = None,
    *,
    warn_issues: bool = True,
    warn_deprecations: bool = True,
    raise_errors: bool = False,
    require_ssh_keys: bool = True,
) -> Config:
    """Load and validate the agentworks configuration.

    Args:
        path: Override config file path (default: ~/.config/agentworks/config.toml).
        warn_issues: Emit config issues as warnings to stderr (default: True).
            Set to False when the caller handles issues itself (e.g. doctor).
        warn_deprecations: Emit deprecation nudges (default: True; also
            silenceable per-invocation via --no-deprecations).
        raise_errors: Raise typed errors for early file failures instead of
            using the legacy stderr and ``SystemExit`` path.
        require_ssh_keys: Fail the load when the operator's SSH key files
            (``operator.ssh_public_key``, ``ssh_private_key``,
            ``extra_ssh_public_keys``) do not exist on disk (default:
            True). Set to False for callers that inspect installed
            vocabulary rather than reach a VM: existence becomes a soft
            entry in ``config.config_issues`` instead of a ``ConfigError``.
            Never lower this for a command that uses the operator's
            identity to connect to or provision anything.

    Returns:
        Validated Config object.

    Raises:
        ConfigError: If the config is missing or invalid.
        SystemExit: If an early file failure occurs and ``raise_errors`` is false.
    """
    # Re-imported here (rather than bound at module load) so that tests'
    # ``monkeypatch.setattr("agentworks.config.CONFIG_PATH", ...)``, which
    # patches the attribute on the public ``agentworks.config`` package, is
    # observed. A module-top `from ... import CONFIG_PATH` would instead
    # bind this module's own copy of the name at import time, permanently
    # deaf to a later monkeypatch.
    from agentworks.config import CONFIG_PATH

    config_path = path or CONFIG_PATH
    if not config_path.exists():
        if raise_errors:
            raise ConfigFileNotFoundError(
                f"configuration file not found: {format_host_path(config_path)}",
                hint="Create it to get started. See the documentation for the schema.",
            )
        print(f"Configuration file not found: {format_host_path(config_path)}", file=sys.stderr)
        print("Run `agw config init` to create one from the commented sample.", file=sys.stderr)
        raise SystemExit(1)

    try:
        raw_text = config_path.read_text()
    except (OSError, UnicodeError) as error:
        if raise_errors:
            raise ConfigError(f"cannot read configuration file {format_host_path(config_path)}: {error}") from None
        raise
    try:
        data = tomllib.loads(raw_text)
    except tomllib.TOMLDecodeError as e:
        if raise_errors:
            raise ConfigError(f"invalid config file {format_host_path(config_path)}: {e}") from None
        print(f"Error: invalid config file {format_host_path(config_path)}: {e}", file=sys.stderr)
        raise SystemExit(1) from None

    # Pre-scan the raw text for section-header line numbers so we can attach
    # ``declared_at: SourceLocation`` to the settings singletons that carry
    # one. tomllib loses this info on parse; the scanner is a small regex
    # pre-pass.
    decls = _SectionLineMap(
        config_path=config_path,
        section_lines=scan_section_lines(raw_text),
    )

    issues: list[str] = []

    if "dotfiles" in data:
        raise ConfigError(
            "[dotfiles] section has been removed. Move dotfiles settings into "
            "[admin.config] (dotfiles_source, dotfiles_destination, dotfiles_install_cmd)."
        )

    _raise_unexpected_top_level_keys(data)

    session_config = _load_session_config(data, issues)

    # No settings loader produces a deprecation today (the last producer,
    # the ``[secret_backends.*]`` no-op nudge, was retired). The channel stays
    # wired because it is generic machinery with an operator-facing flag
    # (--no-deprecations), not because anything currently rides it.
    deprecations: list[str] = []
    secret_config_data = _load_secret_config(data, issues, decls)
    enabled_system_plugins = _load_plugins(data, issues, decls)

    config = Config(
        operator=_load_operator(data, issues, require_ssh_keys=require_ssh_keys),
        paths=_load_paths(data),
        defaults=_load_defaults(data),
        source_path=config_path,
        session=session_config,
        database=_load_database_config(data),
        secret_config_data=secret_config_data,
        enabled_system_plugins=enabled_system_plugins,
        config_issues=tuple(issues),
        deprecation_issues=tuple(deprecations),
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
