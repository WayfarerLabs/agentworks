"""Core's recovery scripts, run for real by a local ``/bin/bash -s`` against stub system commands.

Every command the scripts reach that would touch the system (``getent``,
``dpkg``, ``apt-get``, ``install``, ``sshd``, ``systemctl``, ``tailscale``,
``sleep``) is a stub first on ``PATH`` that records its arguments, so each
test asserts what the script did, and its status, without root. The admin is
the current user, with a scratch home.
"""

from __future__ import annotations

import getpass
import os
import stat
import sys
from dataclasses import dataclass
from functools import partial
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest

from agentworks.capabilities.base import RunContext
from agentworks.capabilities.vm_platform.tailscale_join import log_out
from agentworks.execution import root_script
from agentworks.vms import recovery

if TYPE_CHECKING:
    from pathlib import Path

    from agentworks.capabilities.vm_platform import VMPlatform
    from agentworks.db import VMRow
    from agentworks.execution.root_script import RawRun, RootScriptResult

pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or not os.path.exists("/bin/bash"), reason="the scripts run under /bin/bash"
)

KEY = "tskey-auth-scripts-0123456789"
PUBLIC_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOU3HUqrGubKZio8MVx9WOFPYHGIHl5tIyF3skwoAxv0 operator@host"

_STUB = """#!/bin/bash
name=$(basename "$0")
printf '%s %s\\n' "$name" "$*" >> "{root}/calls"
case "$name" in
    getent) [ "$2" = "{admin}" ] && printf '%s:x:1000:1000::{root}/home:/bin/bash\\n' "$2" && exit 0; exit 2 ;;
    dpkg) exit "$(cat {root}/dpkg-status)" ;;
    sshd) exit "$(cat {root}/sshd-status)" ;;
    systemctl) [ "$1" = is-active ] && exit "$(cat {root}/tailscaled-active)" ;;
    tailscale)
        case "$1" in
            status)
                # One state per read; the last line repeats once the others are used.
                printf '{{"BackendState": "%s"}}\\n' "$(head -n 1 {root}/state)"
                if [ "$(wc -l < {root}/state)" -gt 1 ]; then
                    tail -n +2 {root}/state > {root}/state.next && mv {root}/state.next {root}/state
                fi ;;
            up) cat > {root}/joined-with ;;
            logout) echo "logout output"; exit "$(cat {root}/logout-status)" ;;
        esac ;;
esac
exit 0
"""


@dataclass
class Guest:
    """A scratch root with the stubs, its knobs, and the platform whose channel is a local bash."""

    root: Path

    def set(self, knob: str, value: str) -> None:
        (self.root / knob).write_text(value)

    @property
    def calls(self) -> list[str]:
        path = self.root / "calls"
        return path.read_text().splitlines() if path.exists() else []

    def called(self, command: str) -> list[str]:
        return [call for call in self.calls if call.split(" ", 1)[0] == command]

    @property
    def authorized_keys(self) -> list[str]:
        return (self.root / "home" / ".ssh" / "authorized_keys").read_text().splitlines()

    def run_as_root(self, script: bytes, deadline: float) -> RawRun:
        prefix = f"export PATH={self.root / 'bin'}:$PATH\n".encode()
        return root_script.run_process(["/bin/bash", "-s"], prefix + script, deadline)


class _Platform:
    def __init__(self, guest: Guest, *, needs_guest_sshd: bool) -> None:
        self.guest = guest
        self.recovery_needs_guest_sshd = needs_guest_sshd

    def run_as_root(self, vm: object, ctx: object, script: bytes, deadline: float) -> RawRun:
        return self.guest.run_as_root(script, deadline)


@pytest.fixture
def guest(tmp_path: Path) -> Guest:
    admin = getpass.getuser()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = _STUB.format(root=tmp_path, admin=admin)
    for name in ("getent", "dpkg", "apt-get", "install", "sshd", "systemctl", "tailscale", "sleep"):
        path = bin_dir / name
        path.write_text(stub)
        path.chmod(stat.S_IRWXU)
    (tmp_path / "home").mkdir()
    made = Guest(tmp_path)
    for knob, value in (
        ("dpkg-status", "0"),
        ("sshd-status", "0"),
        ("state", "Running"),
        ("logout-status", "0"),
        ("tailscaled-active", "0"),
    ):
        made.set(knob, value)
    return made


def _vm(admin: str | None = None) -> VMRow:
    return cast("VMRow", SimpleNamespace(name="box", admin_username=admin or getpass.getuser()))


def _platform(guest: Guest, *, needs_guest_sshd: bool = True) -> VMPlatform:
    return cast("VMPlatform", _Platform(guest, needs_guest_sshd=needs_guest_sshd))


def _repair(guest: Guest, *, needs_guest_sshd: bool = True, admin: str | None = None) -> RootScriptResult:
    return recovery.repair(
        _platform(guest, needs_guest_sshd=needs_guest_sshd),
        _vm(admin),
        RunContext(),
        public_key=PUBLIC_KEY,
        auth_key=KEY,
        log=None,
    )


# -- Repair -------------------------------------------------------------------------------------


def test_a_running_node_is_not_joined_and_the_key_is_restored_once(guest: Guest) -> None:
    first = _repair(guest)
    second = _repair(guest)

    assert (first.status, second.status) == (0, 0)
    assert not recovery.joined(first)
    assert guest.called("tailscale") == ["tailscale status --json"] * 2
    assert guest.authorized_keys == [PUBLIC_KEY]


def test_a_node_that_needs_a_login_joins_with_the_key_on_stdin(guest: Guest) -> None:
    guest.set("state", "NeedsLogin")

    result = _repair(guest)

    assert result.status == 0
    assert recovery.joined(result)
    assert guest.called("tailscale")[-1] == "tailscale up --auth-key=file:/dev/stdin"
    assert (guest.root / "joined-with").read_text() == f"{KEY}\n"


def test_a_settling_state_is_read_again_until_it_settles(guest: Guest) -> None:
    guest.set("state", "Starting\nStarting\nNeedsLogin\n")

    result = _repair(guest)

    assert result.status == 0
    assert recovery.joined(result)
    assert guest.called("tailscale") == ["tailscale status --json"] * 3 + ["tailscale up --auth-key=file:/dev/stdin"]


def test_a_state_that_never_settles_fails_after_a_bounded_number_of_reads(guest: Guest) -> None:
    guest.set("state", "NoState\n")

    result = _repair(guest)

    assert result.status == 1
    assert guest.called("tailscale") == ["tailscale status --json"] * recovery.SETTLING_STATE_READS


def test_any_other_state_fails_without_a_join(guest: Guest) -> None:
    guest.set("state", "Stopped")

    result = _repair(guest)

    assert result.status == 1
    assert guest.called("tailscale") == ["tailscale status --json"]


@pytest.mark.parametrize("installed", [True, False])
def test_python3_is_installed_only_when_dpkg_does_not_report_it(guest: Guest, installed: bool) -> None:
    guest.set("dpkg-status", "0" if installed else "1")

    assert _repair(guest).status == 0

    assert guest.called("dpkg") == ["dpkg -s python3"]
    installs = [call for call in guest.called("apt-get") if call.endswith(" install -y python3")]
    assert len(installs) == (0 if installed else 1)


def test_sshd_is_repaired_only_where_the_channel_does_not_use_it(guest: Guest) -> None:
    restart = "systemctl restart ssh.service"

    assert _repair(guest, needs_guest_sshd=True).status == 0
    assert restart not in guest.calls

    assert _repair(guest, needs_guest_sshd=False).status == 0
    assert restart in guest.calls


def test_sshd_is_repaired_before_python3_is_installed(guest: Guest) -> None:
    """A broken sshd gets SSH access back before an install that can take minutes."""
    guest.set("dpkg-status", "1")

    assert _repair(guest, needs_guest_sshd=False).status == 0

    commands = [call.split(" ", 1)[0] for call in guest.calls]
    assert commands.index("sshd") < commands.index("apt-get")


def test_a_key_file_without_a_final_newline_keeps_its_last_key_whole(guest: Guest) -> None:
    keys = guest.root / "home" / ".ssh" / "authorized_keys"
    keys.parent.mkdir()
    keys.write_text("ssh-ed25519 AAAAexisting other@host")

    assert _repair(guest).status == 0

    assert guest.authorized_keys == ["ssh-ed25519 AAAAexisting other@host", PUBLIC_KEY]


@pytest.mark.parametrize("active", [True, False])
def test_tailscaled_is_started_before_its_state_is_read_only_when_it_is_not_running(guest: Guest, active: bool) -> None:
    guest.set("tailscaled-active", "0" if active else "3")

    assert _repair(guest).status == 0

    calls = guest.calls
    started = "systemctl start tailscaled" in calls
    assert started is not active
    if started:
        assert calls.index("systemctl start tailscaled") < calls.index("tailscale status --json")


def test_an_sshd_configuration_that_fails_its_test_is_not_restarted(guest: Guest) -> None:
    guest.set("sshd-status", "255")

    result = _repair(guest, needs_guest_sshd=False)

    assert result.status == 1
    assert "systemctl restart ssh.service" not in guest.calls
    assert guest.called("tailscale") == []


def test_a_missing_admin_account_fails_before_anything_else(guest: Guest) -> None:
    result = _repair(guest, admin="nobody-here")

    assert result.status == 1
    assert [call.split(" ", 1)[0] for call in guest.calls] == ["getent"]


# -- Rekey and logout ---------------------------------------------------------------------------


def _rekey(guest: Guest) -> RootScriptResult:
    return recovery.rekey(_platform(guest), _vm(), RunContext(), auth_key=KEY, log=None)


def test_rekey_restarts_logs_out_joins_and_restarts_with_a_pause_after_each(guest: Guest) -> None:
    result = _rekey(guest)

    assert result.status == 0
    assert [call for call in guest.calls if not call.startswith("sleep")] == [
        "systemctl restart tailscaled",
        "tailscale logout",
        "tailscale up --auth-key=file:/dev/stdin",
        "systemctl restart tailscaled",
    ]
    assert len(guest.called("sleep")) == 4
    assert (guest.root / "joined-with").read_text() == f"{KEY}\n"


def test_rekey_stops_at_the_first_failed_step_with_its_status(guest: Guest) -> None:
    guest.set("logout-status", "3")

    result = _rekey(guest)

    assert result.status == 3
    assert guest.called("tailscale") == ["tailscale logout"]
    assert b"logout output" in result.stdout


@pytest.mark.parametrize("status", [0, 4])
def test_logout_reports_the_logouts_own_status_and_output(guest: Guest, status: int) -> None:
    guest.set("logout-status", str(status))

    platform = _platform(guest)
    result = log_out(partial(platform.run_as_root, _vm(), RunContext()))

    assert result.status == status
    assert guest.called("tailscale") == ["tailscale logout"]
    assert b"logout output" in result.stdout
