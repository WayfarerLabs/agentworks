"""The root channel's conformance vectors, W1 to W9, as one list that two lanes run.

- **L** runs ``root_script.run_process(["/bin/bash", "-s"], ...)`` locally, and
  again under ``sudo -n`` where the runner grants it.
- **V** runs through a live VM's platform ``run_as_root``, from
  ``tests/test_root_channel_live_vectors.py``.

A vector takes the lane and uses only ``lane.run``, so its body is the same
on both. Each asserts observable behavior: a status, bytes, an effect on the
guest. The ``/tmp/agw-conformance-*`` paths are the only ones a vector
creates, and it removes them.
"""

from __future__ import annotations

import time
import uuid
import warnings
from dataclasses import dataclass
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from agentworks.capabilities.vm_platform.tailscale_join import JOIN_FRAGMENT
from agentworks.execution import root_script

if TYPE_CHECKING:
    from collections.abc import Callable

    from agentworks.execution.root_script import RawRun, RootScriptResult

DEADLINE = 60
SECRET = "tskey-auth-conformance-0123456789abcdef"


@dataclass
class RootLane:
    """One way to reach a root shell: ``run_as_root`` and, for V, the platform's registry name.

    ``name`` is ``L``, ``L-sudo`` or ``V``.
    """

    name: str
    run_as_root: Callable[[bytes, float], RawRun]
    platform: str | None = None

    def run(self, payload: str, *, deadline: float = DEADLINE, secret: str | None = None) -> RootScriptResult:
        return root_script.run(self.run_as_root, payload, deadline=deadline, secret=secret)

    def check(self, payload: str) -> bool:
        """Whether a ``test``-style payload exits 0; any other outcome fails the vector."""
        result = self.run(payload)
        assert result.status is not None, result.channel_tail
        return result.status == 0


def scratch_path() -> str:
    return f"/tmp/agw-conformance-{uuid.uuid4().hex[:8]}"


# -- W1, W3 and W5 on every lane ----------------------------------------------------------


def payload_stdin_is_end_of_file_and_a_large_script_runs_whole(lane: RootLane) -> None:
    reads = lane.run("if IFS= read -r line; then echo read; else echo eof; fi\ncat\necho after")

    padding = "\n".join(f": {'x' * 70}" for _ in range(16 * 1024 // 72))
    large = lane.run(f"{padding}\necho whole")

    assert (reads.status, reads.stdout) == (0, b"eof\nafter\n")  # W1
    assert len(padding) > 16_000
    assert (large.status, large.stdout) == (0, b"whole\n")


def the_secret_reaches_the_payload_and_no_argv_or_child_env(lane: RootLane) -> None:
    """The join's own fragment hands the secret to ``tailscale`` with no process's argv holding it.

    ``JOIN_FRAGMENT`` runs as provisioning runs it, against a stub ``tailscale``
    first on ``PATH``, so the real one is never called and a live VM's node is
    untouched. While the stub reads the key, it counts the ``/proc/*/cmdline``
    entries that hold it; ``grep`` reads the needle from a file, so its own
    argv cannot match.
    """
    tmp = scratch_path()
    stub = f"""#!/bin/bash
[ "$1" = up ] || exit 2
case "$2" in --auth-key=file:*) key=$(cat "${{2#--auth-key=file:}}") ;; *) exit 3 ;; esac
printf '%s\\n' "$key" > {tmp}/key
grep -l -F -f {tmp}/key /proc/[0-9]*/cmdline 2>/dev/null | wc -l > {tmp}/argv-holders
"""
    payload = f"""
umask 077
mkdir {tmp}
cat > {tmp}/tailscale <<'AGW_STUB'
{stub}AGW_STUB
chmod 700 {tmp}/tailscale
[ "$AGW_SECRET" = '{SECRET}' ] && echo payload-sees-it
/usr/bin/env | grep -c '^AGW_SECRET=' || true
PATH={tmp}:$PATH
{JOIN_FRAGMENT}
[ "$(cat {tmp}/key)" = "$AGW_SECRET" ] && echo tailscale-read-it
cat {tmp}/argv-holders
rm -rf {tmp}
tr '\\0' ' ' < /proc/$$/cmdline
"""
    result = lane.run(payload, secret=SECRET)

    assert result.status == 0, result.channel_tail
    lines = result.stdout.decode().splitlines()
    assert lines[0] == "payload-sees-it"  # W3
    assert lines[1] == "0"  # no child's environment
    assert lines[2] == "tailscale-read-it"  # the key reached Tailscale whole, on its stdin
    assert lines[3].strip() == "0"  # no process's argv during the join
    assert lines[4].strip() == "/bin/bash -s"


def payload_statuses_come_back_as_themselves(lane: RootLane) -> None:
    statuses = [lane.run(f"exit {status}").status for status in (0, 1, 255)]

    assert statuses == [0, 1, 255]  # W5


# -- W4: a channel that drops ----------------------------------------------------------------


def a_trapped_payload_finishes_after_its_channel_closes(lane: RootLane) -> None:
    """The deadline ends the local end of the channel mid-payload; the guest's payload keeps going.

    The guarantee is the SSH channels' (Lima's ``limactl shell``, Remote Lima, the clouds'), measured
    live on Lima, Remote Lima and Azure. QGA has no connection to drop, and W7 records what WSL2 does.
    """
    if lane.name == "V" and lane.platform not in ("lima", "azure-vm"):
        pytest.skip("W4's live cells are Lima, Remote Lima and Azure")
    flag = scratch_path()
    payload = f"trap '' HUP PIPE\nsleep 3\necho bash-write\n/bin/echo external-write\ntouch {flag}"

    result = lane.run(payload, deadline=1)
    landed = _wait(lambda: lane.check(f"test -e {flag}"))
    lane.run(f"rm -f {flag}")

    assert result.status is None  # W4: the outcome is unknown
    assert landed


def _wait(condition: Callable[[], bool], seconds: float = 20) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(1)
    return condition()


# -- W2: L only --------------------------------------------------------------------------------


def a_script_cut_short_runs_nothing(lane: RootLane) -> None:
    """Every prefix that stops before the closing brace runs nothing, not even the start marker."""
    if lane.name != "L":
        pytest.skip("local lane only")
    flag = scratch_path()
    nonce = "0123456789abcdef" * 2
    script = root_script.wrap(f"touch {flag}", nonce=nonce, secret=SECRET)
    marker = b"\x1e" + nonce.encode()
    closing = script.rindex(b"}")

    for cut in range(closing):
        raw = lane.run_as_root(script[:cut], DEADLINE)

        assert marker not in raw.stdout, cut  # W2
        assert marker not in raw.stderr, cut
    assert not lane.check(f"test -e {flag}")


# -- V only: WSL2 ----------------------------------------------------------------------------


def wsl2_payload_has_no_controlling_terminal(lane: RootLane) -> None:
    """W7's first half; its root-startup-file half is checked by hand, since it edits root's files."""
    if lane.platform != "wsl2":
        pytest.skip("WSL2 only")

    assert lane.check("! { tty -s </dev/tty; } 2>/dev/null")  # W7


def wsl2_records_what_killing_wsl_exe_does_to_the_payload(lane: RootLane) -> None:
    """W7's last half: whether a payload outlives a killed wsl.exe is recorded, not required."""
    if lane.platform != "wsl2":
        pytest.skip("WSL2 only")
    flag = scratch_path()

    lane.run(f"trap '' HUP PIPE\nsleep 3\ntouch {flag}", deadline=1)
    landed = _wait(lambda: lane.check(f"test -e {flag}"), seconds=10)
    lane.run(f"rm -f {flag}")

    outcome = "finished" if landed else "was killed"
    warnings.warn(f"W7: after wsl.exe was killed mid-payload, the payload {outcome}", stacklevel=1)


def wsl2_missing_distro_error_reads_as_text(lane: RootLane) -> None:
    if lane.platform != "wsl2":
        pytest.skip("WSL2 only")
    from types import SimpleNamespace

    from agentworks.capabilities.base import RunContext
    from agentworks.capabilities.vm_platform.wsl2 import WSL2Platform

    platform = WSL2Platform("wsl2", {"name": "wsl2"})
    missing: Any = SimpleNamespace(
        name="missing", platform_metadata={"distro_name": f"agw-conformance-{uuid.uuid4().hex[:8]}"}
    )
    result = root_script.run(
        lambda script, deadline: platform.run_as_root(missing, RunContext(), script, deadline),
        "true",
        deadline=DEADLINE,
    )

    assert result.status is None  # W8
    assert result.channel_tail.strip()
    assert "\0" not in result.channel_tail


# -- Core's recovery scripts: W4's rekey half, W6 and W9 ------------------------------------------


@dataclass
class _LanePlatform:
    """The lane's root channel where a recovery function takes a platform.

    ``prefix`` runs before each script, and ``deadline``, when given, replaces the script's own.
    """

    lane: RootLane
    recovery_needs_guest_sshd: bool = True
    prefix: str = ""
    deadline: float | None = None

    def run_as_root(self, vm: object, ctx: object, script: bytes, deadline: float) -> RawRun:
        return self.lane.run_as_root(self.prefix.encode() + script, self.deadline or deadline)


def _admin(lane: RootLane) -> str:
    """An account the lane's guest has: the VM's admin, or the local user."""
    result = lane.run("id -un")
    assert result.status == 0, result.channel_tail
    return result.stdout.decode().strip()


def _stub_tailscale_and_systemctl(lane: RootLane, tmp: str, *, slow_logout: bool) -> None:
    """Stubs first on ``PATH`` that record each call, so a live VM's node and daemons are untouched."""
    stub = f"""#!/bin/bash
printf '%s %s\\n' "$(basename "$0")" "$*" >> {tmp}/calls
if [ "$(basename "$0")" = tailscale ] && [ "$1" = logout ] && [ -e {tmp}/slow ]; then /bin/sleep 3; fi
if [ "$(basename "$0")" = tailscale ] && [ "$1" = up ]; then cat > {tmp}/joined-with; fi
exit 0
"""
    script = [f"umask 077\nmkdir {tmp}\ncat > {tmp}/tailscale <<'AGW_STUB'\n{stub}AGW_STUB"]
    script += [f"cp {tmp}/tailscale {tmp}/{name}" for name in ("systemctl", "sleep")]
    script += [f"chmod 700 {tmp}/tailscale {tmp}/systemctl {tmp}/sleep"]
    if slow_logout:
        script += [f"touch {tmp}/slow"]
    assert lane.check("\n".join(script))


def the_rekey_script_finishes_after_its_channel_closes_mid_logout(lane: RootLane) -> None:
    """W4's rekey half: the channel closes during the logout step, and the join and the final restart still land."""
    from agentworks.capabilities.base import RunContext
    from agentworks.vms import recovery

    if lane.name == "V" and lane.platform not in ("lima", "azure-vm"):
        pytest.skip("W4's live cells are Lima, Remote Lima and Azure")
    tmp = scratch_path()
    _stub_tailscale_and_systemctl(lane, tmp, slow_logout=True)
    platform: Any = _LanePlatform(lane, prefix=f"export PATH={tmp}:$PATH\n", deadline=1)
    vm: Any = SimpleNamespace(name="conformance", admin_username=_admin(lane))

    result = recovery.rekey(platform, vm, RunContext(), auth_key=SECRET, log=None)
    finished = _wait(lambda: lane.check(f"grep -c 'restart tailscaled' {tmp}/calls | grep -qx 2"))
    calls = lane.run(f"grep -v '^sleep' {tmp}/calls; cat {tmp}/joined-with; rm -rf {tmp}")

    assert result.status is None  # W4: the outcome is unknown
    assert finished
    assert calls.stdout.decode().splitlines() == [
        "systemctl restart tailscaled",
        "tailscale logout",
        "tailscale up --auth-key=file:/dev/stdin",
        "systemctl restart tailscaled",
        SECRET,
    ]


def the_repair_runs_to_status_zero(lane: RootLane) -> None:
    """W6, the repair: on a Debian 12 and a Debian 13 guest whose node is healthy, the repair exits 0 and leaves
    python3 with its standard library. Run it once on a guest prepared with python3 absent and once with only
    ``python3-minimal``; preparing them is by hand, since it removes packages. The rekey and logout scripts change
    the node, so their live cells are ``vm rekey`` and ``vm delete``; on the guest's own Bash they run here against
    stubs."""
    from agentworks.capabilities.base import RunContext
    from agentworks.vms import recovery

    if lane.name != "V":
        pytest.skip("live lane only")
    admin = _admin(lane)
    stand_in_key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOU3HUqrGubKZio8MVx9WOFPYHGIHl5tIyF3skwoAxv0 agw-conformance"
    vm: Any = SimpleNamespace(name="conformance", admin_username=admin)
    platform: Any = _LanePlatform(lane)

    # A healthy node reads Running, so the stand-in key is never used.
    repaired = recovery.repair(platform, vm, RunContext(), public_key=stand_in_key, auth_key=SECRET, log=None)
    lane.run(f"sed -i '/ agw-conformance$/d' ~{admin}/.ssh/authorized_keys")
    stdlib = lane.check("python3 -c 'import json, ssl, venv'")

    tmp = scratch_path()
    _stub_tailscale_and_systemctl(lane, tmp, slow_logout=False)
    stubbed: Any = _LanePlatform(lane, prefix=f"export PATH={tmp}:$PATH\n")
    rekeyed = recovery.rekey(stubbed, vm, RunContext(), auth_key=SECRET, log=None)
    logged_out = recovery.logout(stubbed, vm, RunContext())
    lane.run(f"rm -rf {tmp}")

    assert repaired.status == 0, repaired.stderr  # W6
    assert not recovery.joined(repaired)
    assert stdlib
    assert (rekeyed.status, logged_out.status) == (0, 0)


def backend_state_reads_as_expected(lane: RootLane) -> None:
    """W9: ``BackendState`` reads ``Running`` for a healthy node, and ``NeedsLogin`` for one removed in the admin
    console and for an expired ephemeral key. Set ``AGW_CONFORMANCE_BACKEND_STATE=NeedsLogin`` for those two cells,
    after removing the node or letting its key expire, and recover the VM afterwards."""
    import os

    from agentworks.vms import recovery

    if lane.name != "V":
        pytest.skip("live lane only")
    expected = os.environ.get("AGW_CONFORMANCE_BACKEND_STATE", "Running")

    result = lane.run(recovery.BACKEND_STATE_FRAGMENT)

    assert result.status == 0, result.channel_tail
    assert result.stdout.decode().strip() == expected  # W9


# -- The list ------------------------------------------------------------------------------


@dataclass(frozen=True)
class RootVector:
    name: str
    lanes: str
    check: Callable[[RootLane], None]


ROOT_VECTORS = [
    RootVector("w1-stdin-eof-and-large-script", "LV", payload_stdin_is_end_of_file_and_a_large_script_runs_whole),
    RootVector("w2-cut-short-runs-nothing", "L", a_script_cut_short_runs_nothing),
    RootVector("w3-secret-in-no-argv-or-child-env", "LV", the_secret_reaches_the_payload_and_no_argv_or_child_env),
    RootVector("w4-trapped-payload-outlives-its-channel", "LV", a_trapped_payload_finishes_after_its_channel_closes),
    RootVector("w4-rekey-outlives-its-channel", "LV", the_rekey_script_finishes_after_its_channel_closes_mid_logout),
    RootVector("w5-statuses", "LV", payload_statuses_come_back_as_themselves),
    RootVector("w6-repair-runs-to-zero", "V", the_repair_runs_to_status_zero),
    RootVector("w7-wsl2-no-controlling-terminal", "V", wsl2_payload_has_no_controlling_terminal),
    RootVector("w7-wsl2-killed-wsl-exe-recorded", "V", wsl2_records_what_killing_wsl_exe_does_to_the_payload),
    RootVector("w8-wsl2-missing-distro-reads-as-text", "V", wsl2_missing_distro_error_reads_as_text),
    RootVector("w9-backend-state", "V", backend_state_reads_as_expected),
]


def root_vectors_for(lane: str) -> list[RootVector]:
    return [vector for vector in ROOT_VECTORS if lane in vector.lanes]
