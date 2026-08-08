"""Tests for the install-command loaders.

Covers the per-entry loaders in ``agentworks.install_commands``: the
test-field parsing/validation (``_load_test_fields``) and the required
``command`` field. Built-in payload parity lives in
``test_builtin_entries_parity.py``; Registry-level override behavior lives
there too.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

import pytest

from agentworks.errors import ConfigError
from agentworks.install_commands import (
    SystemInstallCommandEntry,
    UserInstallCommandEntry,
    _load_system_commands,
    _load_user_commands,
)


def test_user_command_preserves_test_exec() -> None:
    entries = _load_user_commands(
        {
            "my-tool": {
                "command": "echo install",
                "description": "My tool",
                "test_exec": "my-tool",
            }
        }
    )
    assert entries["my-tool"].test_exec == "my-tool"


def test_user_command_test_fields_default_none() -> None:
    entries = _load_user_commands({"my-tool": {"command": "echo install", "description": "My tool"}})
    assert entries["my-tool"].test_exec is None
    assert entries["my-tool"].test_file is None
    assert entries["my-tool"].test_dir is None


def test_legacy_test_field_rejected() -> None:
    with pytest.raises(ConfigError, match="'test' is not a valid field"):
        _load_user_commands(
            {
                "old-tool": {
                    "command": "echo install",
                    "description": "Old tool",
                    "test": "old-tool",
                }
            }
        )


@pytest.mark.parametrize("loader", [_load_system_commands, _load_user_commands])
def test_multiple_test_fields_are_loaded(
    loader: Callable[
        [dict[str, object]],
        Mapping[str, SystemInstallCommandEntry | UserInstallCommandEntry],
    ],
) -> None:
    entries = loader(
        {
            "my-tool": {
                "command": "echo install",
                "description": "My tool",
                "test_exec": "my-tool",
                "test_file": "~/.my-tool",
                "test_dir": "~/.my-tool.d",
            }
        }
    )

    entry = entries["my-tool"]
    assert (entry.test_exec, entry.test_file, entry.test_dir) == (
        "my-tool",
        "~/.my-tool",
        "~/.my-tool.d",
    )


def test_system_command_requires_command() -> None:
    with pytest.raises(ConfigError, match="command is required"):
        _load_system_commands({"bad": {"description": "no command"}})
