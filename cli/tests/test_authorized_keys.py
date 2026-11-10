"""SSH authorized_keys reconciliation, through the file program.

The admin's own write runs the real program on the test's paths
(``tests/phase_b_support.py``). The agent's, which is root's work with the
agent as owner, is checked by the operations it asks for; the program's
ownership and link handling under sudo are ``tests/execution/test_file_program.py``'s.
"""

from __future__ import annotations

import stat
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agentworks.config import OperatorConfig
from agentworks.execution.errors import (
    CommandFailed,
    ExecutionIncomplete,
    FileLockTimeout,
    PathRefusal,
    PathRefused,
    RequestRefused,
)
from agentworks.execution.requests import PythonProgram
from agentworks.execution.results import Completed, NotDelivered, NotDeliveredReason, Result, Unknown, UnknownReason
from agentworks.vms.initializer import _reconcile_authorized_keys
from agentworks.vms.initializer.ssh_keys import AuthorizedKeysApplied, AuthorizedKeysUnproven
from tests.phase_b_support import StepGuest, is_file_operation, requires_file_program
from tests.ssh_fixtures import TEST_SSH_PUBLIC_KEY, write_test_ssh_keypair


def _make_config(tmp_path: Path, extra_keys: list[str] | None = None) -> MagicMock:
    """Build a mock Config with real key files on disk."""
    private = tmp_path / "id"
    write_test_ssh_keypair(private)
    pub = tmp_path / "id.pub"

    extra_paths: list[Path] = []
    for i, content in enumerate(extra_keys or []):
        p = tmp_path / f"extra{i}.pub"
        p.write_text(content + "\n")
        extra_paths.append(p)

    operator = OperatorConfig(
        ssh_public_key=pub,
        ssh_private_key=private,
        extra_ssh_public_keys=extra_paths,
    )
    config = MagicMock()
    config.operator = operator
    return config


def _keys(path: Path) -> list[str]:
    return [line for line in path.read_text().splitlines() if line.strip() and not line.startswith("#")]


@requires_file_program
def test_the_admin_file_holds_exactly_the_configured_keys_at_0600(tmp_path: Path) -> None:
    config = _make_config(tmp_path, extra_keys=["ssh-rsa BBBB-extra1", "ssh-ed25519 CCCC-extra2"])
    home = tmp_path / "home"
    guest = StepGuest()

    outcome = _reconcile_authorized_keys(guest.files, config, str(home), guest.log)

    assert isinstance(outcome, AuthorizedKeysApplied)
    keys = home / ".ssh" / "authorized_keys"
    assert _keys(keys) == [TEST_SSH_PUBLIC_KEY.strip(), "ssh-rsa BBBB-extra1", "ssh-ed25519 CCCC-extra2"]
    assert stat.S_IMODE(keys.stat().st_mode) == 0o600
    assert stat.S_IMODE(keys.parent.stat().st_mode) == 0o700
    assert not any(request.elevated for request in guest.access.requests())


@requires_file_program
def test_a_key_no_longer_configured_is_removed(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    keys = tmp_path / "home" / ".ssh" / "authorized_keys"
    keys.parent.mkdir(parents=True)
    keys.write_text("ssh-rsa REMOVED-key\n")
    keys.chmod(0o644)
    guest = StepGuest()

    _reconcile_authorized_keys(guest.files, config, str(tmp_path / "home"), guest.log)

    assert _keys(keys) == [TEST_SSH_PUBLIC_KEY.strip()]
    assert stat.S_IMODE(keys.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    "error",
    [
        CommandFailed("box", 1, Result(Completed(1)), streams_logged=True),
        ExecutionIncomplete("box", Result(Unknown(UnknownReason.CONNECTION))),
        PathRefused("box", "write", "/home/agentworks/.ssh/authorized_keys", PathRefusal.UNSUITABLE),
        RequestRefused("box", "cwd", Result(NotDelivered(NotDeliveredReason.REFUSED, "cwd"))),
        FileLockTimeout("box", "write", "/home/agentworks/.ssh/authorized_keys"),
    ],
    ids=["CommandFailed", "ExecutionIncomplete", "PathRefused", "RequestRefused", "FileLockTimeout"],
)
def test_any_step_failure_of_the_admin_write_leaves_the_identity_unproven(tmp_path: Path, error: Exception) -> None:
    guest = StepGuest(local_files=False)
    guest.access.default_outcome = error

    outcome = _reconcile_authorized_keys(guest.files, _make_config(tmp_path), "/home/agentworks", guest.log)

    assert isinstance(outcome, AuthorizedKeysUnproven)
    (warning,) = guest.log.warned
    assert TEST_SSH_PUBLIC_KEY.split()[1] not in warning


AGENT_HOME = "/home/agt-foo"


def _operation(request: object) -> str | None:
    """The file operation ``request`` runs, or ``None`` for any other request."""
    return request.args[0] if isinstance(request, PythonProgram) and is_file_operation(request) else None


def test_the_agent_s_ssh_directory_and_file_are_placed_as_root_with_the_agent_as_owner(tmp_path: Path) -> None:
    guest = StepGuest(local_files=False)

    outcome = _reconcile_authorized_keys(guest.files, _make_config(tmp_path), AGENT_HOME, guest.log, owner="agt-foo")

    assert isinstance(outcome, AuthorizedKeysApplied)
    (directory, directory_params), (write, write_params) = guest.file_operations()
    assert (directory, write) == ("ensure_directory", "write")
    assert directory_params == {"path": f"{AGENT_HOME}/.ssh", "mode": 0o700, "owner": "agt-foo", "group": "agt-foo"}
    assert (write_params["path"], write_params["mode"]) == (f"{AGENT_HOME}/.ssh/authorized_keys", 0o600)
    assert (write_params["owner"], write_params["group"]) == ("agt-foo", "agt-foo")
    assert all(request.elevated for request in guest.access.requests())
    assert guest.commands() == []


@pytest.mark.parametrize("failing", ["ensure_directory", "write"])
def test_a_failed_agent_placement_raises(tmp_path: Path, failing: str) -> None:
    """Agent SSH depends on the file, so a failure raises and the caller rolls back."""
    guest = StepGuest(local_files=False)
    refused = PathRefused("box", failing, "/home/agt-foo/.ssh", PathRefusal.UNSUITABLE)
    guest.access.answer = lambda call: refused if _operation(call.request) == failing else None

    with pytest.raises(PathRefused):
        _reconcile_authorized_keys(guest.files, _make_config(tmp_path), AGENT_HOME, guest.log, owner="agt-foo")

    assert guest.log.warned == []
