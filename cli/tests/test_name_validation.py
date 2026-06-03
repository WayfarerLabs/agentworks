"""Tests for name validation."""

from __future__ import annotations

import pytest

from agentworks.config import validate_name, validate_name_reference
from agentworks.output import ValidationError


def _is_valid(name: str) -> bool:
    """Return True if validate_name accepts the name."""
    try:
        validate_name(name)
        return True
    except ValidationError:
        return False


def _is_valid_reference(name: str) -> bool:
    """Return True if validate_name_reference accepts the name."""
    try:
        validate_name_reference(name)
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


# -- validate_name_reference (loose) ---------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        # Everything validate_name accepts is also a valid reference.
        "a",
        "abc",
        "dev-vm",
        "my_workspace",
        # The whole point: consecutive hyphens are allowed in references so
        # legacy <workspace>--<agent> names from before validate_name banned
        # them can still be looked up.
        "myws--bot",
        "a--b",
        "ws--with--multiple--dashes",
    ],
)
def test_valid_reference_names(name: str) -> None:
    assert _is_valid_reference(name), f"Expected '{name}' to be a valid reference"


@pytest.mark.parametrize(
    "name,reason",
    [
        # Everything else validate_name rejects, reference also rejects --
        # the only relaxation is the consecutive-hyphen rule.
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
def test_invalid_reference_names(name: str, reason: str) -> None:
    assert not _is_valid_reference(name), (
        f"Expected '{name}' to be an invalid reference ({reason})"
    )
