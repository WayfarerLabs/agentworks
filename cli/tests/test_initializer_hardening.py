"""Tests for VM hardening applied by the initializer.

The sysctl baseline and the fstab ``hidepid`` reconcile run over a recording
admin access whose file operations publish to the test's own paths (see
``tests/phase_b_support.py``), so each test reads what the step left on disk
and which commands it ran. The pure line editor is tested on its own.
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from agentworks.execution.errors import CommandFailed
from agentworks.execution.requests import Command
from agentworks.execution.results import Completed, Result
from agentworks.vms import hardening
from tests.phase_b_support import StepGuest, requires_file_program


def _load() -> Command:
    """Loading the baseline file alone, at the path the fixture gives it."""
    return Command(("sysctl", "-p", hardening.HARDENING_SYSCTL_PATH), elevated=True, deadline=60)


def _remount(value: int | str) -> Command:
    return Command(("mount", "-o", f"remount,hidepid={value}", "/proc"), elevated=True, deadline=60)


@pytest.fixture
def guest(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> StepGuest:
    monkeypatch.setattr(hardening, "HARDENING_SYSCTL_PATH", str(tmp_path / "sysctl.d" / "99-agentworks.conf"))
    monkeypatch.setattr(hardening, "HARDENING_FSTAB_PATH", str(tmp_path / "fstab"))
    return StepGuest()


def _sysctl() -> Path:
    return Path(hardening.HARDENING_SYSCTL_PATH)


def _fstab() -> Path:
    return Path(hardening.HARDENING_FSTAB_PATH)


# -- The sysctl baseline ---------------------------------------------------------


@requires_file_program
def test_sysctl_baseline_is_written_and_reloaded_when_missing(guest: StepGuest) -> None:
    hardening._apply_hardening_sysctl(guest.files, guest.log)

    assert _sysctl().read_text() == hardening.HARDENING_SYSCTL_CONTENT
    assert stat.S_IMODE(_sysctl().stat().st_mode) == 0o644
    assert stat.S_IMODE(_sysctl().parent.stat().st_mode) == 0o755
    assert guest.commands() == [_load()]


@requires_file_program
def test_sysctl_baseline_that_matches_is_neither_written_nor_reloaded(guest: StepGuest) -> None:
    _sysctl().parent.mkdir()
    _sysctl().write_text(hardening.HARDENING_SYSCTL_CONTENT)
    before = _sysctl().stat().st_ino

    hardening._apply_hardening_sysctl(guest.files, guest.log)

    assert guest.writes() == []
    assert guest.commands() == []
    assert _sysctl().stat().st_ino == before


@requires_file_program
def test_sysctl_baseline_that_differs_is_rewritten_and_reloaded(guest: StepGuest) -> None:
    _sysctl().parent.mkdir()
    _sysctl().write_text("kernel.dmesg_restrict = 0\n")

    hardening._apply_hardening_sysctl(guest.files, guest.log)

    assert _sysctl().read_text() == hardening.HARDENING_SYSCTL_CONTENT
    assert guest.commands() == [_load()]


@requires_file_program
def test_a_failed_reload_warns_and_the_fstab_step_still_runs(guest: StepGuest) -> None:
    failed = Result(Completed(1), b"", b"sysctl: cannot stat /proc/sys/x\n")
    guest.respond(_load(), CommandFailed("box", 1, failed, streams_logged=True))
    _fstab().write_text("proc  /proc  proc  defaults,hidepid=1  0  0\n")

    hardening.apply_vm_hardening(guest.files, guest.log)

    assert len(guest.log.warned) == 1
    assert "cannot stat /proc/sys/x" in guest.log.warned[0]
    assert guest.commands() == [_load(), _remount(1)]


@requires_file_program
def test_a_failed_load_restores_the_prior_baseline_and_the_next_run_retries(guest: StepGuest) -> None:
    _sysctl().parent.mkdir()
    _sysctl().write_text("kernel.dmesg_restrict = 0\n")
    guest.respond(_load(), CommandFailed("box", 1, Result(Completed(1)), streams_logged=True))

    hardening.apply_vm_hardening(guest.files, guest.log)

    assert _sysctl().read_text() == "kernel.dmesg_restrict = 0\n"
    guest.access.responses.clear()
    guest.access.calls.clear()

    hardening._apply_hardening_sysctl(guest.files, guest.log)

    assert _sysctl().read_text() == hardening.HARDENING_SYSCTL_CONTENT
    assert guest.commands() == [_load()]


@requires_file_program
def test_a_failed_load_of_a_new_baseline_removes_it(guest: StepGuest) -> None:
    guest.respond(_load(), CommandFailed("box", 1, Result(Completed(1)), streams_logged=True))

    hardening.apply_vm_hardening(guest.files, guest.log)

    assert not _sysctl().exists()


@requires_file_program
def test_a_linked_sysctl_file_is_refused_with_a_warning_and_its_target_left_alone(
    guest: StepGuest, tmp_path: Path
) -> None:
    target = tmp_path / "elsewhere"
    target.write_text("kept\n")
    _sysctl().parent.mkdir()
    _sysctl().symlink_to(target)

    hardening.apply_vm_hardening(guest.files, guest.log)

    assert target.read_text() == "kept\n"
    assert len(guest.log.warned) >= 1
    assert _load() not in guest.commands()


# -- The fstab hidepid edit -----------------------------------------------------------


@requires_file_program
@pytest.mark.parametrize(
    ("before", "after"),
    [
        (
            "UUID=root  /  ext4  defaults  0  1\n",
            f"UUID=root  /  ext4  defaults  0  1\n{hardening.HARDENING_FSTAB_NEW_LINE}\n",
        ),
        (
            "proc  /proc  proc  defaults  0  0\n",
            f"proc  /proc  proc  defaults,hidepid=1  0  0  {hardening.HARDENING_FSTAB_COMMENT}\n",
        ),
        (
            "proc  /proc  proc  defaults,hidepid=0  0  0\n",
            f"proc  /proc  proc  defaults,hidepid=1  0  0  {hardening.HARDENING_FSTAB_COMMENT}\n",
        ),
    ],
    ids=["appended", "added-option", "upgraded"],
)
def test_an_fstab_edit_is_published_keeping_the_files_metadata_and_proc_is_remounted(
    guest: StepGuest, before: str, after: str
) -> None:
    _fstab().write_text(before)
    _fstab().chmod(0o640)

    hardening._apply_hardening_fstab(guest.files, guest.log)

    assert _fstab().read_text() == after
    assert stat.S_IMODE(_fstab().stat().st_mode) == 0o640
    assert guest.commands() == [_remount(1)]


@requires_file_program
@pytest.mark.parametrize(
    ("content", "effective"),
    [
        ("proc  /proc  proc  defaults,hidepid=1  0  0\n", 1),
        ("proc  /proc  proc  defaults,hidepid=noaccess  0  0\n", "noaccess"),
        ("proc  /proc  proc  defaults,hidepid=2  0  0\n", 2),
        ("proc  /proc  proc  defaults,hidepid=invisible  0  0\n", "invisible"),
        ("proc  /proc  proc  defaults,hidepid=bogus  0  0\n", "bogus"),
    ],
    ids=["already-1", "already-noaccess", "stricter-2", "stricter-invisible", "unrecognized"],
)
def test_an_fstab_that_needs_no_edit_is_not_written_and_proc_keeps_its_value(
    guest: StepGuest, content: str, effective: int | str
) -> None:
    _fstab().write_text(content)

    hardening._apply_hardening_fstab(guest.files, guest.log)

    assert guest.writes() == []
    assert _fstab().read_text() == content
    assert guest.commands() == [_remount(effective)]


@requires_file_program
def test_a_malformed_proc_line_warns_leaves_fstab_alone_and_still_remounts(guest: StepGuest) -> None:
    _fstab().write_text("proc  /proc  proc\n")

    hardening._apply_hardening_fstab(guest.files, guest.log)

    assert len(guest.log.warned) == 1
    assert guest.writes() == []
    assert guest.commands() == [_remount(1)]


@requires_file_program
def test_a_missing_fstab_warns_and_skips_the_remount(guest: StepGuest) -> None:
    hardening._apply_hardening_fstab(guest.files, guest.log)

    assert len(guest.log.warned) == 1
    assert not _fstab().exists()
    assert guest.commands() == []


# -- Section role/level shape (regression guard) -------------------------------


@requires_file_program
def test_vm_initialization_step_and_subresult_roles(guest: StepGuest, captured_output) -> None:  # noqa: ANN001
    """Phase-B step lines render as BODY; only genuine sub-results render as DETAIL.

    The captured level is the ambient section level (1) for both roles;
    the step-vs-subresult distinction rides the ROLE (the handler maps
    DETAIL to level + 1 at render time).
    """
    from agentworks import output
    from agentworks.output import Role

    _fstab().write_text("# fstab\nUUID=root  /  ext4  defaults  0  1\n")

    with output.section("VM Initialization"):
        hardening.apply_vm_hardening(guest.files, guest.log)

    assert (Role.BODY, 1, "Applying sysctl baseline...") in captured_output.lines
    assert (Role.BODY, 1, "Ensuring hidepid=1 on /proc...") in captured_output.lines
    assert (Role.DETAIL, 1, "Added /proc entry to /etc/fstab") in captured_output.lines
    assert not any(
        role is Role.DETAIL and msg == "Ensuring hidepid=1 on /proc..." for role, _level, msg in captured_output.lines
    )


# -- Pure function tests for the fstab editor ----------------------------------


def test_ensure_proc_hidepid_appends_when_no_proc_line() -> None:
    from agentworks.vms.hardening import _ensure_proc_hidepid_in_fstab

    content = "UUID=root  /  ext4  defaults  0  1\n"
    new, action, eff = _ensure_proc_hidepid_in_fstab(content)
    assert action == "appended"
    assert eff == "1"
    assert "proc" in new and "hidepid=1" in new


def test_ensure_proc_hidepid_added_option() -> None:
    from agentworks.vms.hardening import _ensure_proc_hidepid_in_fstab

    content = "proc  /proc  proc  defaults  0  0\n"
    new, action, eff = _ensure_proc_hidepid_in_fstab(content)
    assert action == "added-option"
    assert eff == "1"
    assert "hidepid=1" in new


def test_ensure_proc_hidepid_upgraded() -> None:
    from agentworks.vms.hardening import _ensure_proc_hidepid_in_fstab

    content = "proc  /proc  proc  defaults,hidepid=0  0  0\n"
    new, action, eff = _ensure_proc_hidepid_in_fstab(content)
    assert action == "upgraded"
    assert eff == "1"
    assert "hidepid=1" in new
    assert "hidepid=0" not in new


def test_ensure_proc_hidepid_no_op() -> None:
    from agentworks.vms.hardening import _ensure_proc_hidepid_in_fstab

    content = "proc  /proc  proc  defaults,hidepid=1  0  0\n"
    new, action, eff = _ensure_proc_hidepid_in_fstab(content)
    assert action == "no-op"
    assert eff == "1"
    assert new == content


def test_ensure_proc_hidepid_preserves_stricter() -> None:
    from agentworks.vms.hardening import _ensure_proc_hidepid_in_fstab

    content = "proc  /proc  proc  defaults,hidepid=2  0  0\n"
    new, action, eff = _ensure_proc_hidepid_in_fstab(content)
    assert action == "preserved-stricter"
    assert eff == "2"
    assert new == content


def test_ensure_proc_hidepid_preserves_trailing_comment() -> None:
    """An existing trailing comment on the /proc line is preserved on edit."""
    from agentworks.vms.hardening import _ensure_proc_hidepid_in_fstab

    content = "proc  /proc  proc  defaults  0  0  # custom proc note\n"
    new, action, _ = _ensure_proc_hidepid_in_fstab(content)
    assert action == "added-option"
    assert "# custom proc note" in new


def test_ensure_proc_hidepid_malformed() -> None:
    """A /proc line without 6 fields is left untouched (caller warns)."""
    from agentworks.vms.hardening import _ensure_proc_hidepid_in_fstab

    content = "proc  /proc  proc\n"
    new, action, eff = _ensure_proc_hidepid_in_fstab(content)
    assert action == "malformed"
    assert new == content
    assert eff == "1"


def test_ensure_proc_hidepid_upgrades_off_by_name() -> None:
    from agentworks.vms.hardening import _ensure_proc_hidepid_in_fstab

    new, action, eff = _ensure_proc_hidepid_in_fstab("proc  /proc  proc  defaults,hidepid=off  0  0\n")
    assert action == "upgraded"
    assert eff == "1"
    assert "hidepid=off" not in new
    assert new.count("hidepid=") == 1


@pytest.mark.parametrize("value", ["²", "3", "01", "Invisible"])
def test_ensure_proc_hidepid_leaves_a_value_the_kernel_does_not_name_alone(value: str) -> None:
    from agentworks.vms.hardening import _ensure_proc_hidepid_in_fstab

    content = f"proc  /proc  proc  defaults,hidepid={value}  0  0\n"
    new, action, eff = _ensure_proc_hidepid_in_fstab(content)
    assert action == "preserved-unrecognized"
    assert eff == value
    assert new == content


@pytest.mark.parametrize(
    ("options", "action", "effective"),
    [
        ("defaults,hidepid=invisible,nosuid,hidepid=1", "no-op", "1"),
        ("defaults,hidepid=1,nosuid,hidepid=invisible", "preserved-stricter", "invisible"),
    ],
)
def test_ensure_proc_hidepid_reads_the_last_occurrence_as_mount_does(options: str, action: str, effective: str) -> None:
    from agentworks.vms.hardening import _ensure_proc_hidepid_in_fstab

    content = f"proc  /proc  proc  {options}  0  0\n"
    new, got_action, eff = _ensure_proc_hidepid_in_fstab(content)
    assert (got_action, eff) == (action, effective)
    assert new == content
