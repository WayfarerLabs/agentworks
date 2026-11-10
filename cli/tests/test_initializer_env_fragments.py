"""The per-user profile and rc fragments and the rc files' source lines, over the file program.

Each test publishes to its own home on this machine (``tests/phase_b_support.py``).
The system fragments (identity profile, sshd, sudoers) are covered in
``test_initializer_system_fragments.py``.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from agentworks.execution.requests import Command
from agentworks.execution.results import Completed, Result
from agentworks.vms.initializer import (
    AGENTWORKS_PROFILE,
    AGENTWORKS_RC,
    _ensure_agentworks_files_sourced,
    _write_agentworks_profile,
    _write_agentworks_rc,
)
from agentworks.vms.initializer.shell_env import SourceLine, ensure_source_line, source_files
from tests.phase_b_support import StepGuest, requires_file_program

pytestmark = requires_file_program


@pytest.fixture
def home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    return home


def _lines(path: Path) -> list[str]:
    return path.read_text().splitlines()


def test_source_files_name_the_zsh_files_only_for_a_zsh_account() -> None:
    assert source_files("/h", "bash") == {
        AGENTWORKS_PROFILE: ("/h/.profile", "/h/.bashrc"),
        AGENTWORKS_RC: ("/h/.bashrc",),
    }
    assert source_files("/h", "zsh") == {
        AGENTWORKS_PROFILE: ("/h/.profile", "/h/.bashrc", "/h/.zprofile"),
        AGENTWORKS_RC: ("/h/.bashrc", "/h/.zshrc"),
    }


@pytest.mark.parametrize("shell", ["bash", "zsh"])
def test_a_bash_account_gets_no_zsh_files_and_every_named_file_sources_its_fragment(home: Path, shell: str) -> None:
    guest = StepGuest()

    _ensure_agentworks_files_sourced(guest.files, guest.log, home=str(home), shell=shell)

    assert (home / ".zprofile").exists() == (shell == "zsh")
    assert (home / ".zshrc").exists() == (shell == "zsh")
    for fragment, rcs in source_files(str(home), shell).items():
        for rc in rcs:
            assert f". {home}/{fragment}" in _lines(Path(rc))
            assert stat.S_IMODE(Path(rc).stat().st_mode) == 0o644
    assert guest.log.warned == []


def test_a_second_run_writes_nothing(home: Path) -> None:
    _ensure_agentworks_files_sourced(StepGuest().files, StepGuest().log, home=str(home), shell="zsh")
    before = {path.name: path.stat().st_ino for path in home.iterdir()}
    guest = StepGuest()

    _ensure_agentworks_files_sourced(guest.files, guest.log, home=str(home), shell="zsh")

    assert guest.writes() == []
    assert {path.name: path.stat().st_ino for path in home.iterdir()} == before


def test_an_existing_rc_file_keeps_its_content_and_mode_and_gets_the_line_on_its_own(home: Path) -> None:
    rc = home / ".bashrc"
    rc.write_text("alias ll='ls -l'")  # no final newline
    rc.chmod(0o600)

    outcome = ensure_source_line(StepGuest().files, str(rc), AGENTWORKS_RC, f". {home}/{AGENTWORKS_RC}")

    assert outcome is SourceLine.ADDED
    assert _lines(rc) == ["alias ll='ls -l'", f". {home}/{AGENTWORKS_RC}"]
    assert stat.S_IMODE(rc.stat().st_mode) == 0o600


def test_a_file_that_names_the_fragment_is_left_alone(home: Path) -> None:
    rc = home / ".bashrc"
    rc.write_text(f'[ -f "$HOME/{AGENTWORKS_RC}" ] && . "$HOME/{AGENTWORKS_RC}"\n')
    guest = StepGuest()

    assert ensure_source_line(guest.files, str(rc), AGENTWORKS_RC, f". {home}/{AGENTWORKS_RC}") is SourceLine.PRESENT
    assert guest.writes() == []


def _linked_rc(home: Path, target_content: str) -> tuple[Path, Path]:
    target = home / ".dotfiles" / "bashrc"
    target.parent.mkdir()
    target.write_text(target_content)
    rc = home / ".bashrc"
    rc.symlink_to(target)
    return rc, target


class LinkedGuest(StepGuest):
    """Runs the linked-file probes, ``test -L`` and ``grep``, on this machine, as the guest would."""

    def _answer(self, call):  # noqa: ANN001, ANN202
        request = call.request
        if isinstance(request, Command) and request.argv[0] in ("test", "grep"):
            completed = subprocess.run(list(request.argv), capture_output=True, check=False)
            return Result(Completed(completed.returncode))
        return super()._answer(call)


def test_a_linked_rc_file_whose_target_sources_the_fragment_is_accepted_without_a_write(home: Path) -> None:
    lines = f". ~/{AGENTWORKS_PROFILE}\n. ~/{AGENTWORKS_RC}\n"
    rc, target = _linked_rc(home, lines)
    guest = LinkedGuest()

    _ensure_agentworks_files_sourced(guest.files, guest.log, home=str(home), shell="bash")

    assert rc.is_symlink() and target.read_text() == lines
    assert guest.log.warned == []
    assert str(rc) not in guest.writes()
    assert len(guest.log.outputs) == 2


def test_a_linked_rc_file_without_the_line_is_never_written_through_and_warns_with_the_line(home: Path) -> None:
    rc, target = _linked_rc(home, "export EDITOR=vi\n")
    guest = LinkedGuest()

    _ensure_agentworks_files_sourced(guest.files, guest.log, home=str(home), shell="bash")

    assert rc.is_symlink() and target.read_text() == "export EDITOR=vi\n"
    assert str(rc) not in guest.writes()
    # One warning for each fragment .bashrc should source, each naming the line to add.
    assert len(guest.log.warned) == 2
    assert all(f". {home}/" in warning for warning in guest.log.warned)
    assert f". {home}/{AGENTWORKS_PROFILE}" in _lines(home / ".profile")


def test_another_file_type_at_an_rc_file_warns_and_the_other_files_are_still_written(home: Path) -> None:
    (home / ".bashrc").mkdir()
    guest = LinkedGuest()

    _ensure_agentworks_files_sourced(guest.files, guest.log, home=str(home), shell="bash")

    assert (home / ".bashrc").is_dir()
    assert len(guest.log.warned) == 2
    assert f". {home}/{AGENTWORKS_PROFILE}" in _lines(home / ".profile")


@pytest.mark.parametrize("shell", ["sh", "bash", "zsh"])
def test_the_profile_keeps_the_last_of_interleaved_path_entries_so_their_precedence_holds(
    home: Path, shell: str
) -> None:
    shell_path = shutil.which(shell)
    if shell_path is None:
        pytest.skip(f"{shell} is unavailable")
    guest = StepGuest()

    _write_agentworks_profile(
        guest.files,
        ["~/.local/bin", "/opt/tools", "$HOME/.local/bin", "/opt/tools"],
        guest.log,
        home=str(home),
        shell="bash",
        identity_env={"AGENTWORKS_AGENT": "dev"},
    )

    result = subprocess.run(
        [shell_path, "-c", f'. "$HOME/{AGENTWORKS_PROFILE}"; printf "%s\\n" "$PATH" "$AGENTWORKS_AGENT"; umask'],
        env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
        check=True,
        capture_output=True,
        text=True,
    )
    path, agent, umask = result.stdout.splitlines()
    assert (path, agent, int(umask, 8)) == (f"/opt/tools:{home}/.local/bin:/usr/bin:/bin", "dev", 0o027)
    assert stat.S_IMODE((home / AGENTWORKS_PROFILE).stat().st_mode) == 0o644


def test_a_rewritten_profile_drops_paths_no_longer_configured(home: Path) -> None:
    guest = StepGuest()
    _write_agentworks_profile(guest.files, ["/opt/old"], guest.log, home=str(home), shell="bash")

    _write_agentworks_profile(guest.files, [], guest.log, home=str(home), shell="bash")

    result = subprocess.run(
        ["/bin/sh", "-c", f'. "$HOME/{AGENTWORKS_PROFILE}"; printf "%s" "$PATH"'],
        env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout == "/usr/bin:/bin"


def test_the_rc_fragment_is_written_0644_and_sourced_from_the_interactive_rc_files(home: Path) -> None:
    guest = StepGuest()

    _write_agentworks_rc(guest.files, ["export HOOKED=1"], guest.log, home=str(home), shell="zsh")

    assert stat.S_IMODE((home / AGENTWORKS_RC).stat().st_mode) == 0o644
    result = subprocess.run(
        ["/bin/sh", "-c", f'. "$HOME/{AGENTWORKS_RC}"; printf "%s" "$HOOKED"'],
        env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout == "1"
    assert sorted(path.name for path in home.iterdir() if path.name != AGENTWORKS_RC) == [".bashrc", ".zshrc"]


def test_a_linked_fragment_is_refused_and_warned_about(home: Path) -> None:
    elsewhere = home.parent / "elsewhere"
    elsewhere.write_text("not ours\n")
    os.symlink(elsewhere, home / AGENTWORKS_RC)
    guest = StepGuest()

    _write_agentworks_rc(guest.files, [], guest.log, home=str(home), shell="bash")

    assert elsewhere.read_text() == "not ours\n"
    assert len(guest.log.warned) == 1
    assert not (home / ".bashrc").exists()
