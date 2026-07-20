"""Tests for the install-command loaders.

Covers the per-entry loaders in ``agentworks.install_commands``: the
test-field parsing/validation (``_load_test_fields``) and the required
``command`` field. Built-in payload parity lives in
``test_builtin_catalog_parity.py``; Registry-level override behavior lives
there too.
"""

from __future__ import annotations

import pytest

from agentworks.errors import ConfigError
from agentworks.install_commands import _load_system_commands, _load_user_commands


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
    entries = _load_user_commands(
        {"my-tool": {"command": "echo install", "description": "My tool"}}
    )
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


def test_multiple_test_fields_rejected() -> None:
    with pytest.raises(ConfigError, match="at most one"):
        _load_user_commands(
            {
                "bad": {
                    "command": "echo install",
                    "description": "Bad",
                    "test_exec": "bad",
                    "test_file": "~/.bad",
                }
            }
        )


def test_system_command_requires_command() -> None:
    with pytest.raises(ConfigError, match="command is required"):
        _load_system_commands({"bad": {"description": "no command"}})
