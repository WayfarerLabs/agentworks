"""Strict loader for the focused ``[database]`` settings projection."""

from __future__ import annotations

import tomllib
from typing import TYPE_CHECKING

from agentworks.config.loaders_core import _raise_unexpected_keys
from agentworks.config.models import DatabaseConfig
from agentworks.errors import ConfigError
from agentworks.path_rendering import format_host_path

if TYPE_CHECKING:
    from pathlib import Path

_DATABASE_KEYS = {"auto_backup_before_migration"}


def _load_database_config(data: dict[str, object]) -> DatabaseConfig:
    raw = data.get("database", {})
    if not isinstance(raw, dict):
        raise ConfigError("[database] must be a table")
    _raise_unexpected_keys(raw, _DATABASE_KEYS, "database")
    value = raw.get("auto_backup_before_migration", True)
    if type(value) is not bool:
        raise ConfigError("database.auto_backup_before_migration must be a boolean")
    return DatabaseConfig(auto_backup_before_migration=value)


def load_database_config(path: Path | None = None) -> DatabaseConfig:
    """Read only ``[database]``, returning safe defaults for an absent file."""
    from agentworks.config import CONFIG_PATH

    config_path = path or CONFIG_PATH
    if not config_path.exists():
        return DatabaseConfig()
    try:
        with config_path.open("rb") as config_file:
            data = tomllib.load(config_file)
    except (OSError, UnicodeError) as error:
        raise ConfigError(f"cannot read configuration file {format_host_path(config_path)}: {error}") from None
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"invalid config file {format_host_path(config_path)}: {error}") from None
    return _load_database_config(data)
