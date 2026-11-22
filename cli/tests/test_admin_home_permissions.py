"""Admin home-directory isolation: the admin-user follow-up (issue #231) to the
agent home hardening of issue #228. A world-readable admin ``$HOME`` let any
agent user on the VM read the admin's git credentials, shell history, tool
caches, and dotfiles.

Three enforcement points, mirroring the agent side:

1. an admin-side ``chmod 0750`` of the admin home, applied by
   ``_harden_admin_home`` on every ``_phase_b_setup`` (initial provision AND
   reinit), with an ``id -gn`` post-condition guard that warns on a shared
   primary group;
2. a ``umask 027`` line in the admin's managed ``~/.agentworks-profile.sh``,
   emitted by ``profile_fragment`` itself so it survives reinit rewrites
   (``test_initializer_env_fragments.py`` sources it and reads the umask); and
3. ``useradd -m -U`` in the create-time bootstrap script so the admin's primary
   group is a private per-user group.

The steps run over a recording admin access (``tests/phase_b_support.py``);
the bootstrap check is a pure string assertion on the generated script.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from agentworks.capabilities.vm_platform.bootstrap_script import generate_bootstrap_script
from agentworks.execution.errors import CommandFailed, ExecutionIncomplete
from agentworks.execution.requests import Command, Script, Shell
from agentworks.execution.results import Completed, Result, Unknown, UnknownReason
from agentworks.vms.initializer import _harden_admin_home
from agentworks.vms.initializer.shell_env import _set_login_shell
from tests.conftest import requires_posix_shell
from tests.phase_b_support import StepGuest

if TYPE_CHECKING:
    from tests.vm_access_support import Call

ADMIN = "agentworks"
HOME = f"/home/{ADMIN}"


CHMOD = Command(("chmod", "0750", HOME), deadline=60)
GROUP = Command(("id", "-gn", ADMIN), deadline=60)


def _guest(primary_group: str = ADMIN) -> StepGuest:
    guest = StepGuest(local_files=False)
    guest.respond(GROUP, Result(Completed(0), f"{primary_group}\n".encode()))
    return guest


# ---------------------------------------------------------------------------
# chmod 0750 + private-group guard
# ---------------------------------------------------------------------------


def test_harden_admin_home_chmods_0750_as_the_admin_and_then_checks_the_group() -> None:
    """The admin owns its own home, so the chmod is not elevated (contrast the
    agent path, where root chmods a different user's home)."""
    guest = _guest()

    _harden_admin_home(guest.files, home=HOME, admin_username=ADMIN, log=guest.log)

    assert guest.commands() == [CHMOD, GROUP]
    assert guest.log.warned == []


def test_harden_admin_home_shared_primary_group_warns() -> None:
    guest = _guest(primary_group="users")

    _harden_admin_home(guest.files, home=HOME, admin_username=ADMIN, log=guest.log)

    (warning,) = guest.log.warned
    assert "'users'" in warning


@pytest.mark.parametrize(
    "error",
    [
        CommandFailed("box", 1, Result(Completed(1)), streams_logged=True),
        ExecutionIncomplete("box", Result(Unknown(UnknownReason.CONNECTION))),
    ],
)
def test_harden_admin_home_failure_warns_not_raises(error: Exception) -> None:
    guest = _guest()
    guest.respond(CHMOD, error)

    _harden_admin_home(guest.files, home=HOME, admin_username=ADMIN, log=guest.log)

    assert len(guest.log.warned) == 1
    assert GROUP not in guest.commands()


# ---------------------------------------------------------------------------
# Login shell
# ---------------------------------------------------------------------------


@requires_posix_shell
@pytest.mark.parametrize("installed", [True, False])
def test_the_login_shell_is_set_to_the_shell_found_on_path_and_a_missing_one_warns(
    tmp_path: Path, installed: bool
) -> None:
    """The script runs under ``sh`` with a recording ``usermod``; a missing shell never reaches it."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    usermod = bin_dir / "usermod"
    usermod.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > {tmp_path}/usermod-args\n')
    usermod.chmod(0o755)
    if installed:
        (bin_dir / "fakeshell").write_text("#!/bin/sh\n")
        (bin_dir / "fakeshell").chmod(0o755)
    guest = StepGuest(local_files=False)

    def run_script(call: Call) -> Result | BaseException | None:
        if not isinstance(call.request, Script):
            return None
        assert call.request.elevated and call.request.shell is Shell.SH
        completed = subprocess.run(
            ["/bin/sh", "-c", call.request.source], env={"PATH": f"{bin_dir}:/usr/bin:/bin"}, check=False
        )
        result = Result(Completed(completed.returncode))
        return (
            CommandFailed("box", completed.returncode, result, streams_logged=True) if completed.returncode else result
        )

    guest.access.answer = run_script

    _set_login_shell(guest.files, shell="fakeshell", admin_username=ADMIN, log=guest.log)

    if installed:
        assert (tmp_path / "usermod-args").read_text().splitlines() == ["-s", f"{bin_dir}/fakeshell", ADMIN]
        assert guest.log.warned == []
    else:
        assert not (tmp_path / "usermod-args").exists()
        assert len(guest.log.warned) == 1


# ---------------------------------------------------------------------------
# bootstrap useradd -U (create-time private primary group)
# ---------------------------------------------------------------------------


def test_bootstrap_forces_private_group_idempotently() -> None:
    """The create-time bootstrap forces a per-user private primary group
    (``groupadd -f`` + ``useradd -g``) so the admin's primary group is private
    regardless of the image's ``USERGROUPS_ENAB``; without it, the 0750 home
    could leak to a shared primary group. The ``groupadd -f`` + ``-g`` form is
    idempotent, unlike ``useradd -U``, which would abort the whole bootstrap
    under ``set -euo pipefail`` if a group named ``$VM_USER`` already exists."""
    script = generate_bootstrap_script(
        admin_username="agentworks",
        ssh_public_key="ssh-ed25519 AAAA testkey",
        provisioning_packages=["curl"],
        hostname="lima--myvm",
        swap=4,
    )

    assert 'groupadd -f "$VM_USER"' in script
    assert 'useradd -m -g "$VM_USER" -s /bin/bash "$VM_USER"' in script
    # The abort-prone form must not creep back in.
    assert "useradd -m -U" not in script

    # groupadd precedes useradd -g (the group must exist before it is assigned).
    assert script.index('groupadd -f "$VM_USER"') < script.index('useradd -m -g "$VM_USER"')
