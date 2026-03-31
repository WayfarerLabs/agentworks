"""Tests for name validation."""

from __future__ import annotations

import click
import pytest

from agentworks.config import validate_name


def _is_valid(name: str) -> bool:
    """Return True if validate_name accepts the name."""
    try:
        validate_name(name)
        return True
    except (SystemExit, click.exceptions.Exit):
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
