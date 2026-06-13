"""Tests for build_export_block and build_prefixed_command."""

from __future__ import annotations

from agentworks.env import build_export_block, build_prefixed_command


def test_empty_env_returns_empty_string() -> None:
    assert build_export_block({}) == ""


def test_single_entry() -> None:
    assert build_export_block({"A": "1"}) == "export A=1"


def test_multiple_entries_joined_with_and_and() -> None:
    out = build_export_block({"A": "1", "B": "2"})
    assert out == "export A=1 && export B=2"


def test_values_with_spaces_are_quoted() -> None:
    out = build_export_block({"GREET": "hello world"})
    assert out == "export GREET='hello world'"


def test_values_with_shell_metacharacters_are_quoted() -> None:
    """shlex.quote wraps the value in single quotes; metacharacters become literal."""
    out = build_export_block({"BAD": "a; rm -rf /"})
    assert out == "export BAD='a; rm -rf /'"


def test_iteration_order_preserved() -> None:
    """Caller controls precedence by the dict order they pass in."""
    out = build_export_block({"Z": "z", "A": "a", "M": "m"})
    assert out.split(" && ") == ["export Z=z", "export A=a", "export M=m"]


def test_prefixed_command_with_empty_env_returns_command_unchanged() -> None:
    assert build_prefixed_command({}, "exec bash") == "exec bash"


def test_prefixed_command_with_env_prepends_block() -> None:
    out = build_prefixed_command({"A": "1"}, "exec bash")
    assert out == "export A=1 && exec bash"


def test_prefixed_command_with_complex_command() -> None:
    """The command is appended as-is, no quoting or escaping of the command itself."""
    out = build_prefixed_command({"A": "1"}, "cd /workspace && exec bash")
    assert out == "export A=1 && cd /workspace && exec bash"
