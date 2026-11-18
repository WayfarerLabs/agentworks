"""Phase B's system fragments: the identity profile and zprofile block, sshd, sudoers and skel.

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
        ("AGENTWORKS_SSHD_ACCEPT_ENV_PATH", "sshd_config.d/50-agentworks-accept-env.conf"),
        ("AGENTWORKS_SUDOERS_ENV_KEEP_PATH", "sudoers.d/50-agentworks-env-keep"),
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


def _rm(path: Path) -> Command:
    return Command(("rm", "-f", "--", str(path)), elevated=True, deadline=60)


def _installed_accept_env(guest: StepGuest) -> Path:
    """An installed fragment, which the guest's ``rm`` removes; file operations run locally."""
    fragment = _path("AGENTWORKS_SSHD_ACCEPT_ENV_PATH")
    fragment.parent.mkdir()
    fragment.write_bytes(b"AcceptEnv *\n")

    def answer(call: Call) -> Result | BaseException | None:
        if call.request == _rm(fragment):
            fragment.unlink(missing_ok=True)
            return Result(Completed(0))
        return run_file_program_locally(call)

    guest.access.answer = answer
    return fragment


def test_an_installed_accept_env_fragment_is_removed_and_sshd_reloaded(guest: StepGuest) -> None:
    fragment = _installed_accept_env(guest)

    shell_env._remove_sshd_accept_env(guest.files, guest.log)

    assert not fragment.exists()
    assert guest.commands() == [_rm(fragment), SSHD_RELOAD]
    assert all(call.options["check"] for call in guest.access.calls if call.request in (_rm(fragment), SSHD_RELOAD))
    assert guest.log.warned == []


def test_no_accept_env_fragment_leaves_sshd_alone(guest: StepGuest) -> None:
    shell_env._remove_sshd_accept_env(guest.files, guest.log)

    assert guest.commands() == []
    assert guest.writes() == []
    assert guest.log.warned == []


def test_a_failed_removal_warns_and_never_reloads(guest: StepGuest) -> None:
    fragment = _path("AGENTWORKS_SSHD_ACCEPT_ENV_PATH")
    fragment.parent.mkdir()
    fragment.write_bytes(b"AcceptEnv *\n")
    guest.respond(_rm(fragment), _failed(b"rm: cannot remove\n"))

    shell_env._remove_sshd_accept_env(guest.files, guest.log)

    assert guest.commands() == [_rm(fragment)]
    assert len(guest.log.warned) == 1


def test_a_failed_reload_after_the_removal_warns_once(guest: StepGuest) -> None:
    fragment = _installed_accept_env(guest)
    guest.respond(SSHD_RELOAD, _failed(b"Failed to reload ssh.service\n"))

    shell_env._remove_sshd_accept_env(guest.files, guest.log)

    assert not fragment.exists()
    assert guest.commands() == [_rm(fragment), SSHD_RELOAD]
    [warning] = guest.log.warned
    assert str(fragment) in warning


# -- sudoers fragments ------------------------------------------------------------------


def test_the_env_keep_fragment_is_staged_validated_then_promoted_and_the_staging_removed(guest: StepGuest) -> None:
    real = str(_path("AGENTWORKS_SUDOERS_ENV_KEEP_PATH"))
    staging = real + ".tmp"

    shell_env._write_sudoers_env_keep(guest.files, guest.log)

    assert stat.S_IMODE(Path(staging).stat().st_mode) == 0o440
    assert guest.writes() == [staging]
    assert guest.commands() == [
        Command(("visudo", "-cf", staging), elevated=True, deadline=60),
        Command(("mv", "-fT", "--", staging, real), elevated=True, deadline=60),
        Command(("rm", "-f", "--", staging), elevated=True, deadline=60),
    ]
    assert guest.log.warned == []


def test_the_retired_console_setenv_fragment_is_removed_by_one_elevated_rm() -> None:
    guest = StepGuest(local_files=False)

    shell_env._remove_sudoers_console_setenv(guest.files, guest.log)

    [call] = guest.access.calls
    assert call.request == Command(
        ("rm", "-f", "--", "/etc/sudoers.d/51-agentworks-console-setenv"), elevated=True, deadline=60
    )
    assert call.options["check"]
    assert guest.log.warned == []


def test_a_failed_console_setenv_removal_warns_without_raising() -> None:
    guest = StepGuest(local_files=False)
    guest.access.default_outcome = _failed()

    shell_env._remove_sudoers_console_setenv(guest.files, guest.log)

    assert len(guest.log.warned) == 1


def test_a_rejected_sudoers_fragment_never_reaches_its_real_path(guest: StepGuest) -> None:
    real = str(_path("AGENTWORKS_SUDOERS_ENV_KEEP_PATH"))
    staging = real + ".tmp"
    guest.respond(Command(("visudo", "-cf", staging), elevated=True, deadline=60), _failed(b"syntax error\n"))

    shell_env._write_sudoers_env_keep(guest.files, guest.log)

    assert real not in guest.writes()
    assert not any(command.argv[0] == "mv" for command in guest.commands())  # type: ignore[union-attr]
    assert guest.commands()[-1] == Command(("rm", "-f", "--", staging), elevated=True, deadline=60)
    assert len(guest.log.warned) == 1


def test_a_failed_staging_cleanup_keeps_the_rejection_and_is_reported_beside_it(guest: StepGuest) -> None:
    real = str(_path("AGENTWORKS_SUDOERS_ENV_KEEP_PATH"))
    staging = real + ".tmp"
    guest.respond(Command(("visudo", "-cf", staging), elevated=True, deadline=60), _failed(b"syntax error\n"))
    guest.respond(
        Command(("rm", "-f", "--", staging), elevated=True, deadline=60),
        ExecutionIncomplete("box", Result(Unknown(UnknownReason.CONNECTION))),
    )

    shell_env._write_sudoers_env_keep(guest.files, guest.log)

    cleanup, rejection = guest.log.warned
    assert staging in cleanup
    assert "syntax error" in rejection


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
