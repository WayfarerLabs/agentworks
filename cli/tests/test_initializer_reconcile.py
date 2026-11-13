"""Tests for the VM initializer's target-reconciliation helpers.

These helpers all reconcile state on an already-connected VM rather than
provisioning fresh state: preserving SSH host keys across reboots and
repairing the SVE mask on pre-mask Apple-vz VMs (``authorized_keys`` has
``test_authorized_keys.py``). Both run over a recording admin access whose
file operations publish to the test's own paths (``tests/phase_b_support.py``).
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from agentworks.capabilities.vm_platform import bootstrap_script
from agentworks.execution.errors import ExecutionIncomplete
from agentworks.execution.requests import Command, Script, Shell
from agentworks.execution.results import Completed, Result, Unknown, UnknownReason
from agentworks.vms.initializer import _apply_sve_mask, _preserve_ssh_host_keys
from tests.phase_b_support import StepGuest, requires_file_program

SVE_GATE = Script(f"{bootstrap_script.SVE_APPLE_VZ_GREP} && {bootstrap_script.SVE_CPUINFO_GREP}", Shell.SH, deadline=60)
UPDATE_GRUB = Command(("update-grub",), elevated=True, deadline=60)
CMDLINE = Command(("grep", "-qw", "arm64.nosve", "/proc/cmdline"), deadline=60)


@pytest.fixture
def guest(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> StepGuest:
    monkeypatch.setattr(bootstrap_script, "SSH_PRESERVE_KEYS_PATH", str(tmp_path / "cloud.cfg.d" / "99-keys.cfg"))
    monkeypatch.setattr(bootstrap_script, "SVE_NOSVE_GRUB_PATH", str(tmp_path / "grub.d" / "99-nosve.cfg"))
    return StepGuest()


# -- SSH host key preservation ---------------------------------------------


@requires_file_program
def test_preserve_ssh_host_keys_writes_phase_as_dropin_as_root(guest: StepGuest) -> None:
    """The drop-in holds exactly the bytes create-time bootstrap writes, 0644 under a 0755 parent."""
    _preserve_ssh_host_keys(guest.files, guest.log)

    dropin = Path(bootstrap_script.SSH_PRESERVE_KEYS_PATH)
    assert dropin.read_text() == bootstrap_script.SSH_PRESERVE_KEYS_CONTENT
    assert stat.S_IMODE(dropin.stat().st_mode) == 0o644
    assert stat.S_IMODE(dropin.parent.stat().st_mode) == 0o755
    ((operation, _),) = guest.file_operations()
    assert operation == "write"
    assert all(request.elevated for request in guest.access.requests())
    assert guest.log.warned == []


@requires_file_program
def test_preserve_ssh_host_keys_replaces_whatever_the_dropin_held(guest: StepGuest) -> None:
    dropin = Path(bootstrap_script.SSH_PRESERVE_KEYS_PATH)
    dropin.parent.mkdir()
    dropin.write_text("ssh_deletekeys: true\n")

    _preserve_ssh_host_keys(guest.files, guest.log)

    assert dropin.read_text() == bootstrap_script.SSH_PRESERVE_KEYS_CONTENT


def test_preserve_ssh_host_keys_warns_on_failure() -> None:
    """A failure is non-fatal: logged as a warning, no exception raised."""
    guest = StepGuest(local_files=False)
    guest.access.default_outcome = ExecutionIncomplete("box", Result(Unknown(UnknownReason.CONNECTION)))

    _preserve_ssh_host_keys(guest.files, guest.log)

    assert len(guest.log.warned) == 1


# -- Apple-vz SVE mask reconcile (Phase B repair of pre-mask VMs) -----------


def test_apply_sve_mask_noop_when_gate_closed() -> None:
    """Non-Apple host or already-masked VM: gate closed, nothing else runs."""
    guest = StepGuest(local_files=False)
    guest.respond(SVE_GATE, Result(Completed(1)))

    _apply_sve_mask(guest.files, guest.log)

    assert guest.access.requests() == [SVE_GATE]
    assert guest.log.warned == []


@requires_file_program
def test_apply_sve_mask_installs_phase_as_dropin_as_root_and_warns_restart(
    guest: StepGuest, warnings: list[str]
) -> None:
    """Unmasked Apple guest: install the grub drop-in as root, run update-grub,
    and warn that a restart is needed (arm64.nosve not yet on the cmdline)."""
    guest.respond(CMDLINE, Result(Completed(1)))

    _apply_sve_mask(guest.files, guest.log)

    dropin = Path(bootstrap_script.SVE_NOSVE_GRUB_PATH)
    assert dropin.read_text() == bootstrap_script.SVE_NOSVE_GRUB_CONTENT
    assert stat.S_IMODE(dropin.stat().st_mode) == 0o644
    assert guest.commands() == [SVE_GATE, UPDATE_GRUB, CMDLINE]
    assert [operation for operation, _ in guest.file_operations()] == ["write"]
    assert guest.log.warned == []
    # The operator is told to restart; not silently left broken.
    assert len(warnings) == 1


@requires_file_program
def test_apply_sve_mask_on_an_already_booted_mask_says_nothing(guest: StepGuest, warnings: list[str]) -> None:
    _apply_sve_mask(guest.files, guest.log)

    assert guest.commands() == [SVE_GATE, UPDATE_GRUB, CMDLINE]
    assert warnings == []


@requires_file_program
def test_apply_sve_mask_warns_and_stops_on_update_grub_failure(guest: StepGuest) -> None:
    """update-grub failure: warn and return without claiming a restart fixes it."""
    guest.respond(UPDATE_GRUB, Result(Completed(1)))

    _apply_sve_mask(guest.files, guest.log)

    assert len(guest.log.warned) == 1
    # No /proc/cmdline check after a failed update-grub.
    assert guest.commands() == [SVE_GATE, UPDATE_GRUB]


def test_apply_sve_mask_non_fatal_when_a_request_cannot_complete() -> None:
    """A failure past the gate is non-fatal: warns, does not raise (Phase B contract)."""
    guest = StepGuest(local_files=False)
    guest.access.default_outcome = ExecutionIncomplete("box", Result(Unknown(UnknownReason.CONNECTION)))
    guest.respond(SVE_GATE, Result(Completed(0)))

    _apply_sve_mask(guest.files, guest.log)  # must not raise

    assert len(guest.log.warned) == 1
