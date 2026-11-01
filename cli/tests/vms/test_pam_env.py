"""The pam_env alert: the probe's classification of files, its parsing, and the warnings."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from agentworks.vms.manager.pam_env import PAM_ENV_PROBE, parse_pam_env_probe, warn_pam_env_settings

if TYPE_CHECKING:
    from pathlib import Path

HOME_ENTRY = "~/.pam_environment"
SHELLS = [name for name in ("sh", "dash", "bash", "zsh") if shutil.which(name)]


def target(stdout: str = "", returncode: int = 0) -> Any:
    return SimpleNamespace(run=lambda command, **kwargs: SimpleNamespace(stdout=stdout, returncode=returncode))


@pytest.mark.parametrize(
    ("stdout", "expected"),
    [
        ("", ()),
        ("\n\n", ()),
        ("/etc/environment\n", ("/etc/environment",)),
        ("~/.pam_environment\n/etc/environment\n", ("/etc/environment", "~/.pam_environment")),
        (" /etc/security/pam_env.conf \r\n", ("/etc/security/pam_env.conf",)),
        # The guest's text is input: only the known names count.
        ("/etc/default/locale\nFOO=1\n/etc/environment extra\n", ()),
    ],
)
def test_the_probe_output_yields_the_known_files_it_names(stdout: str, expected: tuple[str, ...]) -> None:
    assert parse_pam_env_probe(stdout) == expected


# -- The probe itself, run by each POSIX shell an admin might log in with ----------------
#
# Only the home entry is asserted: the other two files are the host's own /etc.


def run_probe(shell: str, home: Path) -> tuple[str, ...]:
    env = {"HOME": str(home), "PATH": os.environ["PATH"]}
    completed = subprocess.run([shell, "-c", PAM_ENV_PROBE], env=env, capture_output=True, text=True, check=True)
    return parse_pam_env_probe(completed.stdout)


@pytest.mark.skipif(sys.platform == "win32", reason="the probe is for a POSIX guest shell")
@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize(
    ("contents", "reported"),
    [
        (None, False),
        (b"", False),
        (b"\n\n", False),
        (b"# a comment\n\n   \n  # indented comment\n", False),
        (b"# a comment\r\n\r\n   \r\n  # indented\r\n", False),
        (b"FOO=1\n", True),
        (b"# a comment\n\nFOO DEFAULT=1", True),
        (b"  FOO=1\r\n", True),
    ],
    ids=[
        "missing",
        "empty",
        "blank-lines",
        "comments-and-blanks",
        "comments-and-blanks-crlf",
        "setting",
        "setting-without-trailing-newline",
        "indented-setting-crlf",
    ],
)
def test_the_probe_reports_a_file_only_when_it_holds_a_setting(
    shell: str, contents: bytes | None, reported: bool, tmp_path: Path
) -> None:
    if contents is not None:
        (tmp_path / ".pam_environment").write_bytes(contents)

    assert (HOME_ENTRY in run_probe(shell, tmp_path)) is reported


@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0, reason="root reads any file")
@pytest.mark.parametrize("shell", SHELLS)
def test_the_probe_does_not_report_an_unreadable_file(shell: str, tmp_path: Path) -> None:
    unreadable = tmp_path / ".pam_environment"
    unreadable.write_bytes(b"FOO=1\n")
    unreadable.chmod(0)

    assert HOME_ENTRY not in run_probe(shell, tmp_path)


# -- The warnings -------------------------------------------------------------------------


def test_a_file_with_settings_produces_one_warning(captured_output) -> None:
    warn_pam_env_settings(target("/etc/environment\n"), "box")

    assert len(captured_output.warnings) == 1


def test_a_clean_vm_produces_no_warning(captured_output) -> None:
    warn_pam_env_settings(target(""), "box")

    assert captured_output.warnings == []


def test_a_probe_that_did_not_run_to_its_end_warns_instead_of_passing_as_clean(captured_output) -> None:
    warn_pam_env_settings(target("", returncode=255), "box")

    assert len(captured_output.warnings) == 1


def test_a_connection_failure_propagates() -> None:
    def run(command: str, **kwargs: object) -> None:
        raise ConnectionError("lost")

    with pytest.raises(ConnectionError):
        warn_pam_env_settings(SimpleNamespace(run=run), "box")  # type: ignore[arg-type]
