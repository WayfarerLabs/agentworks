"""Tests for name validation."""

from __future__ import annotations

import pytest

from agentworks.agents.grants import MAX_WORKSPACE_NAME_LENGTH, WS_GROUP_PREFIX, workspace_group
from agentworks.agents.manager import AGENT_PREFIX, MAX_AGENT_NAME_LENGTH
from agentworks.agents.manager._common import derive_linux_user
from agentworks.config import validate_admin_username
from agentworks.naming import (
    AZURE_VNET_NAME_MAX_LENGTH,
    LINUX_GROUPNAME_MAX_LENGTH,
    LINUX_USERNAME_MAX_LENGTH,
    MAX_FREEFORM_NAME_LENGTH,
    MAX_SECRET_NAME_LENGTH,
    MAX_SYSTEM_SLUG_LENGTH,
    MAX_VM_NAME_LENGTH,
    validate_name,
)
from agentworks.output import ValidationError
from agentworks.plugins.azure.network import VNET_NAME_SUFFIX
from agentworks.sessions.tmux import AGENT_SOCKET_ROOT, MAX_SESSION_NAME_LENGTH, SUN_PATH_MAX


def _is_valid(name: str, *, allow_double_hyphen: bool = False, max_length: int = MAX_FREEFORM_NAME_LENGTH) -> bool:
    """Return True if validate_name accepts the name at ``max_length``."""
    try:
        validate_name(name, allow_double_hyphen=allow_double_hyphen, max_length=max_length)
        return True
    except ValidationError:
        return False


def _is_valid_secret(name: str) -> bool:
    """Return True if validate_name accepts the name at the secret cap."""
    return _is_valid(name, max_length=MAX_SECRET_NAME_LENGTH)


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
        ("abc\n", "contains trailing newline"),
    ],
)
def test_invalid_names(name: str, reason: str) -> None:
    assert not _is_valid(name), f"Expected '{name}' to be invalid ({reason})"


@pytest.mark.parametrize("username", ["agentworks\n", "agentworks\nroot"])
def test_admin_username_rejects_content_outside_the_valid_name(username: str) -> None:
    with pytest.raises(ValidationError):
        validate_admin_username(username)


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
        ("a" * (MAX_FREEFORM_NAME_LENGTH + 1), "too long"),
    ],
)
def test_double_hyphen_flag_does_not_relax_other_rules(name: str, reason: str) -> None:
    assert not _is_valid(name, allow_double_hyphen=True), (
        f"Expected '{name}' to remain invalid with allow_double_hyphen=True ({reason})"
    )


# -- Per-kind length boundaries --------------------------------------------
#
# Each resource kind has its own cap, derived from its real downstream sink
# (Linux username/group, DNS label, or a table-friendly freeform bound). The
# boundary tests pin ok-at-cap / rejected-over-cap for every kind.


@pytest.mark.parametrize(
    "cap",
    [
        MAX_AGENT_NAME_LENGTH,
        MAX_WORKSPACE_NAME_LENGTH,
        MAX_VM_NAME_LENGTH,
        MAX_SESSION_NAME_LENGTH,
        MAX_FREEFORM_NAME_LENGTH,
        MAX_SECRET_NAME_LENGTH,
    ],
)
def test_name_at_cap_ok_over_cap_rejected(cap: int) -> None:
    assert _is_valid("a" * cap, max_length=cap), f"a name of exactly {cap} chars must pass at cap {cap}"
    assert not _is_valid("a" * (cap + 1), max_length=cap), f"a name of {cap + 1} chars must fail at cap {cap}"


def test_kind_caps_have_expected_values() -> None:
    # Pin the concrete caps so an accidental edit to a derivation is caught.
    assert MAX_AGENT_NAME_LENGTH == 28
    assert MAX_WORKSPACE_NAME_LENGTH == 29
    assert MAX_VM_NAME_LENGTH == 38  # MIN over hostname (42) and Azure vnet (38) sinks
    assert MAX_SESSION_NAME_LENGTH == 34  # AF_UNIX socket path budget
    assert MAX_FREEFORM_NAME_LENGTH == 64
    assert MAX_SECRET_NAME_LENGTH == 253


# -- Derivation invariants (prefix change that would overflow fails here) ---
#
# The agent / workspace caps exist so the DERIVED Linux username / group fits
# the 32-char OS limit. Pin the derived length at the cap so a prefix change
# (or a cap bump) that would push the identifier over 32 fails a test rather
# than producing over-limit usernames on the VM.


def test_agent_cap_yields_max_length_username() -> None:
    username = derive_linux_user("a" * MAX_AGENT_NAME_LENGTH)
    assert username == AGENT_PREFIX + "a" * MAX_AGENT_NAME_LENGTH
    assert len(username) == LINUX_USERNAME_MAX_LENGTH == 32


def test_workspace_cap_yields_max_length_group() -> None:
    group = workspace_group("a" * MAX_WORKSPACE_NAME_LENGTH)
    assert group == WS_GROUP_PREFIX + "a" * MAX_WORKSPACE_NAME_LENGTH
    assert len(group) == LINUX_GROUPNAME_MAX_LENGTH == 32


def test_vm_cap_yields_max_length_azure_vnet_name() -> None:
    # The binding VM-name sink is the Azure virtual-network name
    # {slug}-{vm}-vnet (capped at 64), not the {slug}-{vm} hostname. Pin the
    # worst-case composed vnet name at exactly 64 so a slug-length change or a
    # change to VNET_NAME_SUFFIX (imported from azure_vm, the real source) that
    # would overflow the vnet name fails here rather than opaquely on Azure.
    slug = "s" * MAX_SYSTEM_SLUG_LENGTH
    vm_name = "v" * MAX_VM_NAME_LENGTH
    vnet_name = f"{slug}-{vm_name}{VNET_NAME_SUFFIX}"
    assert len(vnet_name) == AZURE_VNET_NAME_MAX_LENGTH == 64


def test_session_cap_yields_max_length_socket_path() -> None:
    # Session names embed in the per-agent tmux AF_UNIX socket path. Pin the
    # worst-case path (longest agent username = the 32-char Linux ceiling) at
    # exactly the usable sun_path length (107) so a socket-root change, or a
    # username-cap change, that would push the path past the bindable limit
    # fails here rather than opaquely with "File name too long" on the VM.
    username = "a" * LINUX_USERNAME_MAX_LENGTH  # longest possible agent username
    session_name = "s" * MAX_SESSION_NAME_LENGTH
    socket_path = f"{AGENT_SOCKET_ROOT}/{username}/{session_name}.sock"
    assert len(socket_path) == SUN_PATH_MAX - 1 == 107


# -- Secret-name length cap (issue #275) -----------------------------------
#
# Secret names are never derived into Linux usernames, so they use the larger
# MAX_SECRET_NAME_LENGTH cap rather than the username-driven caps.


def test_secret_cap_is_larger_than_agent_cap() -> None:
    assert MAX_SECRET_NAME_LENGTH > MAX_AGENT_NAME_LENGTH


def test_secret_name_over_agent_cap_passes() -> None:
    # A name longer than the agent username cap but within the secret cap.
    name = "s" * (MAX_AGENT_NAME_LENGTH + 10)
    assert len(name) > MAX_AGENT_NAME_LENGTH
    assert _is_valid_secret(name), "Expected a name over the agent cap but within the secret cap to be valid"


def test_secret_cap_error_message_reports_secret_max() -> None:
    with pytest.raises(ValidationError) as excinfo:
        validate_name("s" * (MAX_SECRET_NAME_LENGTH + 1), max_length=MAX_SECRET_NAME_LENGTH)
    message = str(excinfo.value)
    assert f"max {MAX_SECRET_NAME_LENGTH}" in message


def test_secret_cap_does_not_relax_character_rules() -> None:
    # Only the length cap is raised for secrets; every character rule holds.
    assert not _is_valid_secret("s" * 40 + "--" + "s" * 40)  # consecutive hyphens
    assert not _is_valid_secret("-" + "s" * 40)  # leading hyphen
    assert not _is_valid_secret("s" * 40 + "-")  # trailing hyphen
    assert not _is_valid_secret("s" * 20 + "." + "s" * 20)  # dot


def test_git_token_secret_from_realistic_credential_name_passes() -> None:
    # Regression for issue #275: git-token-<credential-name> for a reasonable
    # 23-char credential name is a 33-char secret name that must now pass.
    secret_name = "git-token-github-fg-wf-agw-tester"
    assert len(secret_name) == 33
    assert _is_valid_secret(secret_name)
    # And it is still rejected under the tighter agent (username-bearing) cap.
    assert not _is_valid(secret_name, max_length=MAX_AGENT_NAME_LENGTH)


def test_forgotten_cap_defaults_to_freeform_not_a_username_cap() -> None:
    # A caller that forgets to pass max_length gets the generous freeform
    # bound (64), never a silently-wrong OS-derived cap. A 40-char name (over
    # every username/group cap, under freeform) must pass the default.
    name = "a" * 40
    assert MAX_AGENT_NAME_LENGTH < len(name) < MAX_FREEFORM_NAME_LENGTH
    assert _is_valid(name)  # uses the default cap
