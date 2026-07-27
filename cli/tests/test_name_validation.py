"""Tests for name validation."""

from __future__ import annotations

import pytest

from agentworks.config import validate_name
from agentworks.output import ValidationError


def _is_valid(name: str, *, allow_double_hyphen: bool = False) -> bool:
    """Return True if validate_name accepts the name."""
    try:
        validate_name(name, allow_double_hyphen=allow_double_hyphen)
        return True
    except ValidationError:
        return False


# -- Valid names -----------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "a",
        "abc",
        "a1",
        "dev-vm",
        "my_workspace",
        "ws-task-123",
        "a-b-c",
        "a_b_c",
        "a-b_c-d",
        "0abc",
        "abc0",
        "123",
    ],
)
def test_valid_names(name: str) -> None:
    assert _is_valid(name), f"Expected '{name}' to be valid"


# -- Invalid names ---------------------------------------------------------


@pytest.mark.parametrize(
    "name,reason",
    [
        ("", "empty string"),
        ("-abc", "starts with hyphen"),
        ("abc-", "ends with hyphen"),
        ("_abc", "starts with underscore"),
        ("abc_", "ends with underscore"),
        ("a--b", "consecutive hyphens (agent separator)"),
        ("my--workspace", "consecutive hyphens"),
        ("a.b", "contains dot"),
        ("my.vm", "contains dot"),
        ("ABC", "uppercase"),
        ("Dev-VM", "mixed case"),
        ("my workspace", "contains space"),
        ("my@vm", "contains special character"),
        ("a/b", "contains slash"),
    ],
)
def test_invalid_names(name: str, reason: str) -> None:
    assert not _is_valid(name), f"Expected '{name}' to be invalid ({reason})"


# -- Single character edge cases -------------------------------------------


def test_single_letter() -> None:
    assert _is_valid("a")


def test_single_digit() -> None:
    assert _is_valid("0")


def test_single_hyphen() -> None:
    assert not _is_valid("-")


def test_single_underscore() -> None:
    assert not _is_valid("_")


# -- allow_double_hyphen=True (reference paths) ----------------------------


@pytest.mark.parametrize(
    "name",
    [
        # Everything strict-mode accepts is still accepted in loose mode.
        "a",
        "abc",
        "dev-vm",
        "my_workspace",
        # The whole point: legacy <workspace>--<agent> names predating the
        # strict rule must still be referenceable. The DB is the arbiter of
        # existence; the validator only sanitizes characters.
        "myws--bot",
        "a--b",
        "ws--with--multiple--dashes",
    ],
)
def test_double_hyphen_allowed_when_flag_set(name: str) -> None:
    assert _is_valid(name, allow_double_hyphen=True), f"Expected '{name}' to validate with allow_double_hyphen=True"


@pytest.mark.parametrize(
    "name,reason",
    [
        # Loose mode still rejects everything that's character-unsafe; the
        # only relaxation is the consecutive-hyphen rule.
        ("", "empty string"),
        ("-abc", "starts with hyphen"),
        ("abc-", "ends with hyphen"),
        ("ABC", "uppercase"),
        ("a.b", "contains dot"),
        ("a/b", "contains slash"),
        ("a b", "contains space"),
        ("my@vm", "contains special character"),
        ("a" * 31, "too long"),
    ],
)
def test_double_hyphen_flag_does_not_relax_other_rules(name: str, reason: str) -> None:
    assert not _is_valid(name, allow_double_hyphen=True), (
        f"Expected '{name}' to remain invalid with allow_double_hyphen=True ({reason})"
    )
