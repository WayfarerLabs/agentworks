"""The system- and user-install-command spec models.

The two kinds are field-identical and share one authored spec base, so
every assertion below runs against both: a rule that held for only one of
them would be a rule an operator could not predict.
"""

from __future__ import annotations

import pytest

from agentworks.install_commands import SystemInstallCommandEntry, UserInstallCommandEntry

from ._specs import WHERE, decode, rejection

_KINDS = [
    ("system-install-command", SystemInstallCommandEntry),
    ("user-install-command", UserInstallCommandEntry),
]


@pytest.mark.parametrize(("kind", "row_cls"), _KINDS)
def test_every_field_round_trips(kind: str, row_cls: type) -> None:
    spec = {"command": "curl -fsSL https://example.test | sh", "path": ["~/.local/bin"], "test_exec": "example"}

    assert decode(kind, "example", dict(spec), description="install example") == row_cls(
        name="example", description="install example", declared_at=WHERE, **spec
    )


@pytest.mark.parametrize(("kind", "row_cls"), _KINDS)
def test_the_optional_fields_default(kind: str, row_cls: type) -> None:
    row = decode(kind, "example", {"command": "true"})

    assert (row.path, row.test_exec, row.test_file, row.test_dir, row.description) == ([], None, None, None, None)


# -- What an operator reads when it is wrong ----------------------------------


@pytest.mark.parametrize(("kind", "row_cls"), _KINDS)
def test_a_missing_command_says_it_is_required(kind: str, row_cls: type) -> None:
    assert rejection(kind, "example", {}) == f"res.yaml:7: {kind}/example.command: is required"


@pytest.mark.parametrize(("kind", "row_cls"), _KINDS)
def test_two_tests_are_refused_by_name(kind: str, row_cls: type) -> None:
    spec = {"command": "true", "test_exec": "example", "test_dir": "~/example"}

    assert rejection(kind, "example", spec) == (
        f"res.yaml:7: {kind}/example: at most one of test_exec, test_file, test_dir may be set"
    )


@pytest.mark.parametrize(("kind", "row_cls"), _KINDS)
def test_a_bare_test_key_keeps_its_remedy(kind: str, row_cls: type) -> None:
    """``test`` is the mistake operators actually make. As a plain unknown
    key it would name the valid fields but not say which of the three to
    reach for, so it keeps its own steer."""
    assert rejection(kind, "example", {"command": "true", "test": "example"}) == (
        f"res.yaml:7: {kind}/example: 'test' is not a valid field. Use 'test_exec', 'test_file', or 'test_dir'."
    )


@pytest.mark.parametrize(("kind", "row_cls"), _KINDS)
def test_an_unknown_key_names_the_fields_that_are_valid(kind: str, row_cls: type) -> None:
    assert rejection(kind, "example", {"command": "true", "cmd": "true"}) == (
        f"res.yaml:7: {kind}/example.cmd: unknown field; expected one of: command, path, test_dir, test_exec, test_file"
    )


@pytest.mark.parametrize(("kind", "row_cls"), _KINDS)
def test_a_non_string_command_is_no_longer_coerced(kind: str, row_cls: type) -> None:
    """The loader spelled ``str(...)`` around this, so ``command: 7``
    installed the string "7". Strict mode says what it is instead."""
    assert rejection(kind, "example", {"command": 7}) == f"res.yaml:7: {kind}/example.command: must be a string"
