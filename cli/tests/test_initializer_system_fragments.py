"""Phase B's system fragments: the identity profile and zprofile block, sshd, sudoers and skel.

Each step runs over a recording admin access whose file operations publish
to the test's own paths (``tests/phase_b_support.py``), so a test reads what
the step left on disk and which commands it ran, in order.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

from agentworks.capabilities.vm_platform.skel import BASHRC, ZSHRC
from agentworks.execution.errors import CommandFailed, ExecutionIncomplete
from agentworks.execution.requests import Command
from agentworks.execution.results import Completed, Result, Unknown, UnknownReason
from agentworks.vms.initializer import shell_env
from tests.phase_b_support import StepGuest, requires_file_program

pytestmark = requires_file_program

BEGIN, END = "# agentworks-identity-begin", "# agentworks-identity-end"


@pytest.fixture
def guest(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> StepGuest:
    for name, relative in (
        ("AGENTWORKS_IDENTITY_PROFILE_PATH", "profile.d/agentworks-identity.sh"),
        ("AGENTWORKS_ZPROFILE_PATH", "zsh/zprofile"),
        ("AGENTWORKS_SSHD_ACCEPT_ENV_PATH", "sshd_config.d/50-agentworks-accept-env.conf"),
        ("AGENTWORKS_SUDOERS_ENV_KEEP_PATH", "sudoers.d/50-agentworks-env-keep"),
        ("AGENTWORKS_SUDOERS_CONSOLE_SETENV_PATH", "sudoers.d/51-agentworks-console-setenv"),
        ("SKEL_BASHRC_PATH", "skel/.bashrc"),
        ("SKEL_ZSHRC_PATH", "skel/.zshrc"),
    ):
        monkeypatch.setattr(shell_env, name, str(tmp_path / relative))
    return StepGuest()


def _path(name: str) -> Path:
    return Path(getattr(shell_env, name))


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


# -- sshd AcceptEnv ---------------------------------------------------------------------

SSHD_TEST = Command(("sshd", "-t"), elevated=True, deadline=60)
SSHD_RELOAD = Command(("systemctl", "reload", "ssh"), elevated=True, deadline=60)


def test_a_new_sshd_fragment_is_written_validated_then_reloaded(guest: StepGuest) -> None:
    shell_env._write_sshd_accept_env(guest.files, guest.log)

    fragment = _path("AGENTWORKS_SSHD_ACCEPT_ENV_PATH")
    assert fragment.read_bytes().endswith(b"AcceptEnv *\n")
    assert stat.S_IMODE(fragment.stat().st_mode) == 0o644
    assert guest.commands() == [SSHD_TEST, SSHD_RELOAD]
    assert guest.log.warned == []


def test_an_unchanged_sshd_fragment_is_neither_validated_nor_reloaded(guest: StepGuest) -> None:
    shell_env._write_sshd_accept_env(guest.files, guest.log)
    fragment = _path("AGENTWORKS_SSHD_ACCEPT_ENV_PATH")
    before = fragment.stat().st_ino
    guest.access.calls.clear()

    shell_env._write_sshd_accept_env(guest.files, guest.log)

    assert guest.commands() == []
    assert guest.writes() == []
    assert fragment.stat().st_ino == before


def test_a_rejected_sshd_fragment_restores_the_prior_content_and_never_reloads(guest: StepGuest) -> None:
    fragment = _path("AGENTWORKS_SSHD_ACCEPT_ENV_PATH")
    fragment.parent.mkdir()
    fragment.write_bytes(b"AcceptEnv LANG\n")
    guest.respond(SSHD_TEST, _failed(b"sshd: bad configuration\n"))

    shell_env._write_sshd_accept_env(guest.files, guest.log)

    assert fragment.read_bytes() == b"AcceptEnv LANG\n"
    assert guest.commands() == [SSHD_TEST]
    assert len(guest.log.warned) == 1
    assert list(fragment.parent.iterdir()) == [fragment]


def test_a_rejected_new_sshd_fragment_is_removed_and_never_reloaded(guest: StepGuest) -> None:
    guest.respond(SSHD_TEST, _failed())

    shell_env._write_sshd_accept_env(guest.files, guest.log)

    fragment = _path("AGENTWORKS_SSHD_ACCEPT_ENV_PATH")
    assert not fragment.exists()
    assert guest.commands() == [SSHD_TEST]
    assert len(guest.log.warned) == 1


# -- sudoers fragments ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("write", "name"),
    [
        (lambda guest: shell_env._write_sudoers_env_keep(guest.files, guest.log), "AGENTWORKS_SUDOERS_ENV_KEEP_PATH"),
        (
            lambda guest: shell_env._write_sudoers_console_setenv(guest.files, guest.log, "agentworks"),
            "AGENTWORKS_SUDOERS_CONSOLE_SETENV_PATH",
        ),
    ],
    ids=["env_keep", "console_setenv"],
)
def test_a_sudoers_fragment_is_staged_validated_then_promoted_and_the_staging_removed(
    guest: StepGuest, write: object, name: str
) -> None:
    real = str(_path(name))
    staging = real + ".tmp"

    write(guest)  # type: ignore[operator]

    assert stat.S_IMODE(Path(staging).stat().st_mode) == 0o440
    assert guest.writes() == [staging]
    assert guest.commands() == [
        Command(("visudo", "-cf", staging), elevated=True, deadline=60),
        Command(("mv", "-fT", "--", staging, real), elevated=True, deadline=60),
        Command(("rm", "-f", "--", staging), elevated=True, deadline=60),
    ]
    assert guest.log.warned == []


def test_a_rejected_sudoers_fragment_never_reaches_its_real_path(guest: StepGuest) -> None:
    real = str(_path("AGENTWORKS_SUDOERS_ENV_KEEP_PATH"))
    staging = real + ".tmp"
    guest.respond(Command(("visudo", "-cf", staging), elevated=True, deadline=60), _failed(b"syntax error\n"))

    shell_env._write_sudoers_env_keep(guest.files, guest.log)

    assert real not in guest.writes()
    assert not any(command.argv[0] == "mv" for command in guest.commands())  # type: ignore[union-attr]
    assert guest.commands()[-1] == Command(("rm", "-f", "--", staging), elevated=True, deadline=60)
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
