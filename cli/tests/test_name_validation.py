"""Tests for name validation."""

from __future__ import annotations

import pytest

from agentworks.config import MAX_NAME_LENGTH, MAX_SECRET_NAME_LENGTH, validate_name
from agentworks.output import ValidationError


def _is_valid(name: str, *, allow_double_hyphen: bool = False) -> bool:
    """Return True if validate_name accepts the name."""
    try:
        validate_name(name, allow_double_hyphen=allow_double_hyphen)
        return True
    except ValidationError:
        return False


def _is_valid_secret(name: str) -> bool:
    """Return True if validate_name accepts the name at the secret cap."""
    try:
        validate_name(name, max_length=MAX_SECRET_NAME_LENGTH)
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


# -- Secret-name length cap (issue #275) -----------------------------------
#
# Secret names are never derived into Linux usernames, so they use the larger
# MAX_SECRET_NAME_LENGTH cap rather than the username-driven MAX_NAME_LENGTH.


def test_secret_cap_is_larger_than_default() -> None:
    assert MAX_SECRET_NAME_LENGTH > MAX_NAME_LENGTH


def test_secret_name_over_default_cap_passes() -> None:
    # A name longer than the 30-char username cap but within the secret cap.
    name = "s" * (MAX_NAME_LENGTH + 10)
    assert len(name) > MAX_NAME_LENGTH
    assert _is_valid_secret(name), "Expected a >30 name within the secret cap to be valid for secrets"


def test_secret_name_at_secret_cap_passes() -> None:
    assert _is_valid_secret("s" * MAX_SECRET_NAME_LENGTH)


def test_secret_name_over_secret_cap_fails() -> None:
    assert not _is_valid_secret("s" * (MAX_SECRET_NAME_LENGTH + 1))


def test_secret_cap_error_message_reports_secret_max() -> None:
    with pytest.raises(ValidationError) as excinfo:
        validate_name("s" * (MAX_SECRET_NAME_LENGTH + 1), max_length=MAX_SECRET_NAME_LENGTH)
    message = str(excinfo.value)
    assert f"max {MAX_SECRET_NAME_LENGTH}" in message
    assert f"max {MAX_NAME_LENGTH}" not in message


def test_secret_cap_does_not_relax_character_rules() -> None:
    # Only the length cap is raised for secrets; every character rule holds.
    assert not _is_valid_secret("s" * 40 + "--" + "s" * 40)  # consecutive hyphens
    assert not _is_valid_secret("-" + "s" * 40)  # leading hyphen
    assert not _is_valid_secret("s" * 40 + "-")  # trailing hyphen
    assert not _is_valid_secret("s" * 20 + "." + "s" * 20)  # dot


def test_username_bearing_kinds_still_cap_at_default() -> None:
    # The default (username-bearing) cap is unchanged: a >30 name is rejected.
    over = "a" * (MAX_NAME_LENGTH + 1)
    assert not _is_valid(over), "Expected a >30 name to remain invalid at the default cap"
    with pytest.raises(ValidationError) as excinfo:
        validate_name(over)
    assert f"max {MAX_NAME_LENGTH}" in str(excinfo.value)


def test_git_token_secret_from_realistic_credential_name_passes() -> None:
    # Regression for issue #275: git-token-<credential-name> for a reasonable
    # 23-char credential name is a 33-char secret name that must now pass.
    secret_name = "git-token-github-fg-wf-agw-tester"
    assert len(secret_name) == 33
    assert len(secret_name) > MAX_NAME_LENGTH
    assert _is_valid_secret(secret_name)
    # And it is still rejected under the default username-bearing cap.
    assert not _is_valid(secret_name)
