"""Phase B's system fragments: the identity profile and zprofile block, skel, and the retired sshd and sudoers ones.

Each step runs over a recording admin access whose file operations publish
to the test's own paths (``tests/phase_b_support.py``), so a test reads what
the step left on disk and which commands it ran, in order.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from agentworks.capabilities.vm_platform.skel import BASHRC, ZSHRC
from agentworks.execution.errors import CommandFailed, ExecutionIncomplete
from agentworks.execution.requests import Command, PythonProgram
from agentworks.execution.results import Completed, Result, Unknown, UnknownReason
from agentworks.vms.initializer import shell_env
from tests.phase_b_support import StepGuest, is_file_operation, requires_file_program
from tests.vm_access_support import Call, run_file_program_locally

pytestmark = requires_file_program

BEGIN, END = "# agentworks-identity-begin", "# agentworks-identity-end"


@pytest.fixture
def guest(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> StepGuest:
    for name, relative in (
        ("AGENTWORKS_IDENTITY_PROFILE_PATH", "profile.d/agentworks-identity.sh"),
        ("AGENTWORKS_ZPROFILE_PATH", "zsh/zprofile"),
        ("RETIRED_SSHD_ACCEPT_ENV_PATH", "sshd_config.d/50-agentworks-accept-env.conf"),
        ("RETIRED_SSHD_ACCEPT_ENV_RELOAD_PENDING_PATH", "agentworks-accept-env.reload-pending"),
        ("SKEL_BASHRC_PATH", "skel/.bashrc"),
        ("SKEL_ZSHRC_PATH", "skel/.zshrc"),
    ):
        monkeypatch.setattr(shell_env, name, str(tmp_path / relative))
    return StepGuest()


def _path(name: str) -> Path:
    return Path(getattr(shell_env, name))


def _file_operation(request: object) -> tuple[str, str] | None:
    """``(operation, path)`` for a file operation, or ``None`` for any other request."""
    if not isinstance(request, PythonProgram) or not is_file_operation(request):
        return None
    return request.args[0], str(json.loads(request.args[1])["path"])


def _failed(stderr: bytes = b"") -> CommandFailed:
    return CommandFailed("box", 1, Result(Completed(1), b"", stderr), streams_logged=True)


# -- The identity profile and the zprofile block ---------------------------------------


def test_the_identity_profile_exports_each_value_to_a_login_shell(guest: StepGuest) -> None:
    identity = {"AGENTWORKS_VM": "vm-1", "AGENTWORKS_VM_HOST": "my host; $(touch /tmp/x)"}

    shell_env._write_agentworks_identity_profile(guest.files, identity, guest.log)

    profile = _path("AGENTWORKS_IDENTITY_PROFILE_PATH")
    assert stat.S_IMODE(profile.stat().st_mode) == 0o644
    sourced = subprocess.run(
        ["/bin/sh", "-c", '. "$1"; printf "%s\\n%s" "$AGENTWORKS_VM" "$AGENTWORKS_VM_HOST"', "sh", str(profile)],
        capture_output=True,
        text=True,
        check=True,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
    )
    assert sourced.stdout.split("\n") == ["vm-1", "my host; $(touch /tmp/x)"]
    assert guest.log.warned == []


def test_an_absent_zprofile_and_its_directory_are_created_with_the_block_alone(guest: StepGuest) -> None:
    shell_env._write_agentworks_identity_profile(guest.files, {"AGENTWORKS_VM": "vm-1"}, guest.log)

    zprofile = _path("AGENTWORKS_ZPROFILE_PATH")
    assert zprofile.read_text() == f"{BEGIN}\nexport AGENTWORKS_VM=vm-1\n{END}\n"
    assert stat.S_IMODE(zprofile.stat().st_mode) == 0o644
    assert stat.S_IMODE(zprofile.parent.stat().st_mode) == 0o755


def test_a_prior_block_is_replaced_and_the_rest_of_the_file_kept_with_its_metadata(guest: StepGuest) -> None:
    zprofile = _path("AGENTWORKS_ZPROFILE_PATH")
    zprofile.parent.mkdir()
    zprofile.write_text(f"# package default\n{BEGIN}\nexport AGENTWORKS_VM=old\n{END}\nexport OPERATOR=1\n")
    zprofile.chmod(0o640)

    shell_env._write_agentworks_identity_profile(guest.files, {"AGENTWORKS_VM": "vm-2"}, guest.log)

    assert zprofile.read_text() == (
        f"# package default\nexport OPERATOR=1\n{BEGIN}\nexport AGENTWORKS_VM=vm-2\n{END}\n"
    )
    assert stat.S_IMODE(zprofile.stat().st_mode) == 0o640


@pytest.mark.parametrize("orphan", [BEGIN, END])
def test_one_unmatched_marker_warns_and_appends_leaving_the_rest_alone(
    guest: StepGuest, warnings: list[str], orphan: str
) -> None:
    zprofile = _path("AGENTWORKS_ZPROFILE_PATH")
    zprofile.parent.mkdir()
    before = f"{orphan}\nexport OPERATOR=1"  # and no final newline
    zprofile.write_text(before)

    shell_env._write_agentworks_identity_profile(guest.files, {"AGENTWORKS_VM": "vm-1"}, guest.log)

    assert zprofile.read_text() == f"{before}\n{BEGIN}\nexport AGENTWORKS_VM=vm-1\n{END}\n"
    assert len(warnings) == 1


def test_an_end_marker_inside_a_line_does_not_pair_with_a_begin_marker(guest: StepGuest, warnings: list[str]) -> None:
    """Only a line starting with a marker counts, as for the removal, so nothing after the begin marker is lost."""
    zprofile = _path("AGENTWORKS_ZPROFILE_PATH")
    zprofile.parent.mkdir()
    before = f"{BEGIN}\nexport OPERATOR=1\necho 'see {END}'\n"
    zprofile.write_text(before)

    shell_env._write_agentworks_identity_profile(guest.files, {"AGENTWORKS_VM": "vm-1"}, guest.log)

    assert zprofile.read_text() == f"{before}{BEGIN}\nexport AGENTWORKS_VM=vm-1\n{END}\n"
    assert len(warnings) == 1


def test_a_zprofile_that_already_holds_the_block_is_not_rewritten(guest: StepGuest) -> None:
    shell_env._write_agentworks_identity_profile(guest.files, {"AGENTWORKS_VM": "vm-1"}, guest.log)
    zprofile = _path("AGENTWORKS_ZPROFILE_PATH")
    before = zprofile.stat().st_ino
    first = guest.writes()

    shell_env._write_agentworks_identity_profile(guest.files, {"AGENTWORKS_VM": "vm-1"}, guest.log)

    assert zprofile.stat().st_ino == before
    assert guest.writes()[len(first) :] == [str(_path("AGENTWORKS_IDENTITY_PROFILE_PATH"))]


def test_a_linked_zprofile_is_refused_with_a_warning_and_its_target_left_alone(
    guest: StepGuest, tmp_path: Path
) -> None:
    target = tmp_path / "dotfiles-zprofile"
    target.write_text("# the operator's\n")
    zprofile = _path("AGENTWORKS_ZPROFILE_PATH")
    zprofile.parent.mkdir()
    zprofile.symlink_to(target)

    shell_env._write_agentworks_identity_profile(guest.files, {"AGENTWORKS_VM": "vm-1"}, guest.log)

    assert target.read_text() == "# the operator's\n"
    assert zprofile.is_symlink()
    assert len(guest.log.warned) == 1


# -- The retired sshd AcceptEnv fragment -------------------------------------------------

SSHD_RELOAD = Command(("systemctl", "reload", "ssh"), elevated=True, deadline=60)


def _retire(fragment: Path, pending: Path) -> Command:
    return Command(("mv", "-f", "-T", "--", str(fragment), str(pending)), elevated=True, deadline=60)


def _rm(path: Path) -> Command:
    return Command(("rm", "-f", "--", str(path)), elevated=True, deadline=60)


def _accept_env_guest(guest: StepGuest, *, installed: bool = True) -> tuple[Path, Path]:
    """The fragment, installed or not, and its reload-pending name; ``mv``, ``rm`` and file operations act here."""
    fragment, pending = _path("RETIRED_SSHD_ACCEPT_ENV_PATH"), _path("RETIRED_SSHD_ACCEPT_ENV_RELOAD_PENDING_PATH")
    fragment.parent.mkdir()
    if installed:
        fragment.write_bytes(b"AcceptEnv *\n")

    def answer(call: Call) -> Result | BaseException | None:
        if call.request == _retire(fragment, pending):
            fragment.replace(pending)
            return Result(Completed(0))
        if call.request == _rm(pending):
            pending.unlink(missing_ok=True)
            return Result(Completed(0))
        return run_file_program_locally(call)

    guest.access.answer = answer
    return fragment, pending


def test_an_installed_accept_env_fragment_is_removed_and_sshd_reloaded(guest: StepGuest) -> None:
    fragment, pending = _accept_env_guest(guest)

    shell_env._remove_sshd_accept_env(guest.files, guest.log)

    assert not fragment.exists() and not pending.exists()
    assert guest.commands() == [_retire(fragment, pending), SSHD_RELOAD, _rm(pending)]
    assert all(call.options["check"] for call in guest.access.calls if isinstance(call.request, Command))
    assert guest.log.warned == []


def test_no_accept_env_fragment_leaves_sshd_alone(guest: StepGuest) -> None:
    _accept_env_guest(guest, installed=False)

    shell_env._remove_sshd_accept_env(guest.files, guest.log)

    assert guest.commands() == []
    assert guest.writes() == []
    assert guest.log.warned == []


def test_a_failed_removal_warns_and_never_reloads(guest: StepGuest) -> None:
    fragment, pending = _path("RETIRED_SSHD_ACCEPT_ENV_PATH"), _path("RETIRED_SSHD_ACCEPT_ENV_RELOAD_PENDING_PATH")
    fragment.parent.mkdir()
    fragment.write_bytes(b"AcceptEnv *\n")
    guest.respond(_retire(fragment, pending), _failed(b"mv: cannot move\n"))

    shell_env._remove_sshd_accept_env(guest.files, guest.log)

    assert guest.commands() == [_retire(fragment, pending)]
    assert len(guest.log.warned) == 1


@pytest.mark.parametrize(
    "outcome",
    [_failed(b"Failed to reload ssh.service\n"), ExecutionIncomplete("box", Result(Unknown(UnknownReason.DEADLINE)))],
    ids=["failed", "unknown"],
)
def test_a_reload_that_does_not_happen_after_the_removal_is_retried_by_the_next_run(
    guest: StepGuest, outcome: BaseException
) -> None:
    fragment, pending = _accept_env_guest(guest)
    guest.respond(SSHD_RELOAD, outcome)

    shell_env._remove_sshd_accept_env(guest.files, guest.log)

    assert not fragment.exists() and pending.exists()
    [warning] = guest.log.warned
    assert str(fragment) in warning

    guest.access.responses.clear()
    guest.access.calls.clear()
    shell_env._remove_sshd_accept_env(guest.files, guest.log)

    assert guest.commands() == [SSHD_RELOAD, _rm(pending)]
    assert not pending.exists()


# -- The retired sudoers fragments --------------------------------------------------------


def test_the_retired_sudoers_fragments_are_removed_by_one_elevated_rm() -> None:
    guest = StepGuest(local_files=False)

    shell_env._remove_retired_sudoers_fragments(guest.files, guest.log)

    [call] = guest.access.calls
    # The paths earlier releases installed, spelled out: a VM still holds them whatever this module calls them.
    assert call.request == Command(
        (
            "rm",
            "-f",
            "--",
            "/etc/sudoers.d/50-agentworks-env-keep",
            "/etc/sudoers.d/51-agentworks-console-setenv",
            "/etc/sudoers.d/50-agentworks-env-keep.tmp",
            "/etc/sudoers.d/51-agentworks-console-setenv.tmp",
        ),
        elevated=True,
        deadline=60,
    )
    assert call.options["check"]
    assert guest.log.warned == []


def test_a_failed_sudoers_removal_warns_without_raising() -> None:
    guest = StepGuest(local_files=False)
    guest.access.default_outcome = _failed()

    shell_env._remove_retired_sudoers_fragments(guest.files, guest.log)

    assert len(guest.log.warned) == 1


# -- /etc/skel seeds ----------------------------------------------------------------------


def test_skel_seeds_replace_both_files_root_template_mode(guest: StepGuest) -> None:
    bashrc = _path("SKEL_BASHRC_PATH")
    bashrc.parent.mkdir()
    bashrc.write_text("# Debian's stock skel\n")

    shell_env._write_skel_seeds(guest.files, guest.log)

    assert (bashrc.read_text(), _path("SKEL_ZSHRC_PATH").read_text()) == (BASHRC, ZSHRC)
    assert stat.S_IMODE(bashrc.stat().st_mode) == 0o644
    assert stat.S_IMODE(_path("SKEL_ZSHRC_PATH").stat().st_mode) == 0o644
    assert [operation for operation, _ in guest.file_operations()] == ["write", "write"]


def test_a_failed_skel_seed_write_warns_without_raising() -> None:
    guest = StepGuest(local_files=False)
    guest.access.default_outcome = ExecutionIncomplete("box", Result(Unknown(UnknownReason.CONNECTION)))

    shell_env._write_skel_seeds(guest.files, guest.log)

    assert len(guest.log.warned) == 1
