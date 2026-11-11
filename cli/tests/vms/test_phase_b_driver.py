"""Phase B, both its sections, driven whole over a recording admin access.

Every request Phase B makes runs on the access, and its file operations are
answered from memory (nothing touches this machine's paths). What
run_initialization makes of a warning or a raise (partial or failed) is
covered by ``test_applied_state_checkpoint.py``.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest

from agentworks.capabilities.vm_platform.bootstrap_script import SSH_PRESERVE_KEYS_PATH
from agentworks.capabilities.vm_platform.cloud_init import INIT_SYSTEM_PACKAGES
from agentworks.debian import DebianRelease
from agentworks.errors import StateError
from agentworks.execution.errors import (
    CommandFailed,
    ExecutionIncomplete,
    FileConflict,
    FileLockTimeout,
    PathRefusal,
    PathRefused,
    RequestRefused,
    TrustMismatch,
    TrustMissing,
    VMNotReady,
)
from agentworks.execution.requests import Bytes, Command, PythonProgram, Script
from agentworks.execution.results import Completed, NotDelivered, NotDeliveredReason, Result, Unknown, UnknownReason
from agentworks.install_commands import SystemInstallCommandEntry, UserInstallCommandEntry
from agentworks.sessions.tmux import AGENT_SOCKET_ROOT
from agentworks.vms.admin import AdminConfig
from agentworks.vms.hardening import HARDENING_FSTAB_NEW_LINE, HARDENING_FSTAB_PATH
from agentworks.vms.initializer import driver
from agentworks.vms.initializer.packages import apt_install, install_run
from agentworks.vms.initializer.shell_env import AGENTWORKS_ZPROFILE_PATH, SKEL_BASHRC_PATH, SKEL_ZSHRC_PATH
from agentworks.vms.templates import ResolvedVMTemplate
from tests.phase_b_support import StepGuest, is_file_operation
from tests.test_tailscale_dns import DNS_PROBE, READ_RESOLV, RESOLVED_ACTIVE

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from agentworks.db import Database
    from agentworks.execution.requests import RunRequest
    from tests.vm_access_support import Call

HOME = "/home/agentworks"
PROFILE = f"{HOME}/.agentworks-profile.sh"


def _file_operation(request: RunRequest) -> tuple[str, str] | None:
    """``(operation, path)`` for a file operation, or ``None`` for any other request."""
    if not isinstance(request, PythonProgram) or not is_file_operation(request):
        return None
    return request.args[0], str(json.loads(request.args[1])["path"])


def _is_write_of(path: str) -> Callable[[RunRequest], bool]:
    def matches(request: RunRequest) -> bool:
        operation = _file_operation(request)
        return operation is not None and operation[1] == path and operation[0] != "read"

    return matches


def _is(expected: RunRequest) -> Callable[[RunRequest], bool]:
    return lambda request: request == expected


ADMIN_GROUP = Command(("id", "-gn", "agentworks"), deadline=60)


class Guest(StepGuest):
    """A steady guest: a hardened ``/etc/fstab``, a private admin group and empty files; all else succeeds."""

    def __init__(self) -> None:
        super().__init__(local_files=False)
        self.failures: list[tuple[Callable[[RunRequest], bool], BaseException]] = []
        self.access.answer = self._steady

    def fail(self, matches: Callable[[RunRequest], bool], error: BaseException) -> None:
        self.failures.append((matches, error))

    def _steady(self, call: Call) -> Result | BaseException | None:
        for matches, error in self.failures:
            if matches(call.request):
                return error
        if _file_operation(call.request) == ("read", HARDENING_FSTAB_PATH):
            return Result(Completed(0), f"{HARDENING_FSTAB_NEW_LINE}\n".encode())
        if call.request == ADMIN_GROUP:
            return Result(Completed(0), b"agentworks\n")
        return None


@pytest.fixture
def run_section(db: Database, monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    db.insert_vm("box", site="lima-local", hostname="box")
    monkeypatch.setattr("agentworks.vms.sites.site_platform_name", lambda site, registry: "lima")
    kinds: dict[str, dict[str, Any]] = {
        "system-install-command": {
            "sys-tool": SystemInstallCommandEntry(name="sys-tool", description="", command="install-sys-tool")
        },
        "user-install-command": {
            "user-tool": UserInstallCommandEntry(
                name="user-tool", description="", command="install-user-tool", path=["/opt/user-tool/bin"]
            )
        },
    }
    registry: Any = MagicMock()
    registry.iter_kind_items.side_effect = lambda kind: iter(kinds.get(kind, {}).items())
    config: Any = MagicMock()
    config.paths.vm_workspaces = "/opt/agentworks/workspaces"

    def run(
        guest: Guest,
        admin: AdminConfig | None = None,
        operation: driver.VMInitializationOperation = driver.VMInitializationOperation.VM_REINIT,
    ) -> None:
        log: Any = guest.log
        driver._phase_b_setup(
            db,
            config,
            registry,
            ResolvedVMTemplate(name="default", apt=["jq"], system_install_commands=["sys-tool"]),
            admin or AdminConfig(user_install_commands=["user-tool"]),
            "box",
            guest.files,
            (),
            HOME,
            "agentworks",
            log,
            debian_release=DebianRelease.TRIXIE,
            operation=operation,
        )

    return run


def test_a_steady_section_runs_every_step_without_a_warning(run_section: Callable[..., None]) -> None:
    guest = Guest()

    run_section(guest)

    assert guest.log.warned == []
    # The section's last step, the agent socket root, ran (on this guest's empty probe it is rebuilt).
    assert Command(("chmod", "2771", "--", AGENT_SOCKET_ROOT), elevated=True, deadline=60) in guest.commands()


def test_skel_seeds_and_the_zprofile_block_are_written_after_apt_installs(
    run_section: Callable[..., None],
) -> None:
    """apt's ``--force-confnew`` would replace them with the packages' defaults if they came first."""
    guest = Guest()

    run_section(guest)

    requests = guest.access.requests()
    installs = [
        index
        for index, request in enumerate(requests)
        if request in (apt_install(INIT_SYSTEM_PACKAGES), apt_install(["jq"]))
    ]
    seeds = [
        index
        for index, request in enumerate(requests)
        if any(_is_write_of(path)(request) for path in (SKEL_BASHRC_PATH, SKEL_ZSHRC_PATH, AGENTWORKS_ZPROFILE_PATH))
    ]
    assert len(installs) == 2 and len(seeds) == 3
    assert max(installs) < min(seeds)


def _local_dotfiles(tmp_path: Path) -> AdminConfig:
    (tmp_path / "dots").mkdir()
    (tmp_path / "dots" / "install.sh").write_text("#!/bin/sh\n")
    return AdminConfig(user_install_commands=["user-tool"], dotfiles_source=str(tmp_path / "dots"))


def test_the_admin_s_dotfiles_are_not_synced_over_its_workspace_tmuxinator_links(
    run_section: Callable[..., None], tmp_path: Path
) -> None:
    """An absolute destination passes load; the admin's set refuses it when initialization expands it."""
    guest = Guest()
    admin = _local_dotfiles(tmp_path).model_copy(update={"dotfiles_destination": f"{HOME}/.config"})

    run_section(guest, admin)

    assert not any(
        isinstance(request, Script) and "AGW_TREE_DEST" in request.env for request in guest.access.requests()
    )
    assert len(guest.log.warned) == 1


def test_no_request_carries_an_environment_but_apts_and_the_tree_upload_s(
    run_section: Callable[..., None], tmp_path: Path
) -> None:
    """Provisioning is hermetic: identity reaches installs only through the profile fragments."""
    guest = Guest()

    run_section(guest, _local_dotfiles(tmp_path))

    requests = guest.access.requests()
    assert install_run("bash", "install-sys-tool") in requests
    assert install_run("bash", "install-user-tool") in requests
    assert Command(("bash", "-lc", "./install.sh"), cwd=f"{HOME}/.dotfiles", deadline=120) in requests
    environments = {tuple(sorted(request.env)) for request in requests}
    assert environments == {(), ("DEBIAN_FRONTEND",), ("AGW_TREE_DEST", "AGW_TREE_TAG")}
    assert guest.log.warned == []


def test_the_source_lines_are_ensured_after_the_dotfiles_install(
    run_section: Callable[..., None], tmp_path: Path
) -> None:
    """A dotfiles installer that replaces an rc file cannot leave it without its source lines."""
    guest = Guest()

    run_section(guest, _local_dotfiles(tmp_path))

    requests = guest.access.requests()
    install = requests.index(Command(("bash", "-lc", "./install.sh"), cwd=f"{HOME}/.dotfiles", deadline=120))
    bashrc = [
        index for index, request in enumerate(requests) if _file_operation(request) == ("read", f"{HOME}/.bashrc")
    ]
    assert bashrc and max(bashrc) > install


def _written(request: RunRequest) -> str:
    assert isinstance(request, PythonProgram) and isinstance(request.stdin, Bytes)
    return request.stdin.data.partition(b"\n")[2].decode()


def test_on_a_first_create_the_user_install_commands_profile_holds_mise_s_shims_behind_their_paths(
    run_section: Callable[..., None],
) -> None:
    """The profile the install commands' login shells source is complete before they run, as the agent's is."""
    guest = Guest()

    run_section(guest, operation=driver.VMInitializationOperation.VM_CREATE)

    requests = guest.access.requests()
    install = requests.index(install_run("bash", "install-user-tool"))
    (profile,) = [_written(request) for request in requests[:install] if _is_write_of(PROFILE)(request)]
    exports = [line for line in profile.splitlines() if line.startswith("export PATH=")]
    assert exports == [f'export PATH="{HOME}/.local/share/mise/shims:$PATH"', 'export PATH="/opt/user-tool/bin:$PATH"']
    # Written once, so a steady reinit does not rewrite it after the install commands.
    assert not any(_is_write_of(PROFILE)(request) for request in requests[install:])


def test_the_admin_writes_its_fragments_first_and_ensures_their_lines_around_its_install_commands(
    run_section: Callable[..., None], tmp_path: Path
) -> None:
    """The agent's order: the fragments before the dotfiles install, whose login shell reads them; their
    source lines again after it and mise, before the user install commands; and a final pass after those."""
    guest = Guest()

    run_section(guest, _local_dotfiles(tmp_path))

    requests = guest.access.requests()
    profile = next(index for index, request in enumerate(requests) if _is_write_of(PROFILE)(request))
    rc = next(index for index, request in enumerate(requests) if _is_write_of(f"{HOME}/.agentworks-rc.sh")(request))
    dotfiles = requests.index(Command(("bash", "-lc", "./install.sh"), cwd=f"{HOME}/.dotfiles", deadline=120))
    install = requests.index(install_run("bash", "install-user-tool"))
    reads = [
        index for index, request in enumerate(requests) if _file_operation(request) == ("read", f"{HOME}/.profile")
    ]
    assert profile < rc < dotfiles < install
    assert any(dotfiles < index < install for index in reads)
    assert max(reads) > install


@pytest.mark.parametrize(
    "error",
    [
        CommandFailed("box", 1, Result(Completed(1)), streams_logged=True),
        ExecutionIncomplete("box", Result(Unknown(UnknownReason.CONNECTION))),
        PathRefused("box", "write", SSH_PRESERVE_KEYS_PATH, PathRefusal.UNSUITABLE),
        RequestRefused("box", "cwd", Result(NotDelivered(NotDeliveredReason.REFUSED, "cwd"))),
        FileConflict("box", "write", SSH_PRESERVE_KEYS_PATH),
        FileLockTimeout("box", "write", SSH_PRESERVE_KEYS_PATH),
    ],
    ids=["CommandFailed", "ExecutionIncomplete", "PathRefused", "RequestRefused", "FileConflict", "FileLockTimeout"],
)
def test_a_failed_step_warns_and_every_later_step_still_runs(
    run_section: Callable[..., None], error: BaseException
) -> None:
    steady = Guest()
    run_section(steady)
    guest = Guest()
    guest.fail(_is_write_of(SSH_PRESERVE_KEYS_PATH), error)

    run_section(guest)

    assert len(guest.log.warned) == 1
    assert guest.access.requests() == steady.access.requests()


def _dns_latched(guest: Guest) -> None:
    guest.respond(DNS_PROBE, Result(Completed(2)))
    guest.respond(READ_RESOLV, Result(Completed(0), b"# resolv.conf(5) file generated by tailscale\n"))
    guest.respond(RESOLVED_ACTIVE, Result(Completed(0)))


@pytest.mark.parametrize(
    ("stop", "raised"),
    [
        (
            lambda guest: guest.fail(
                _is(apt_install(INIT_SYSTEM_PACKAGES)),
                CommandFailed("box", 100, Result(Completed(100)), streams_logged=True),
            ),
            CommandFailed,
        ),
        (_dns_latched, StateError),
        (lambda guest: guest.fail(_is_write_of(SSH_PRESERVE_KEYS_PATH), TrustMissing("box")), TrustMissing),
        (lambda guest: guest.fail(_is_write_of(SSH_PRESERVE_KEYS_PATH), TrustMismatch("box")), TrustMismatch),
        (
            lambda guest: guest.fail(
                _is_write_of(SSH_PRESERVE_KEYS_PATH), VMNotReady("box", NotDeliveredReason.PYTHON_MISSING)
            ),
            VMNotReady,
        ),
    ],
    ids=["system-packages", "dns-latch", "TrustMissing", "TrustMismatch", "VMNotReady"],
)
def test_what_stops_initialization_is_raised_not_warned(
    run_section: Callable[..., None], stop: Callable[[Guest], None], raised: type[BaseException]
) -> None:
    guest = Guest()
    stop(guest)

    with pytest.raises(raised):
        run_section(guest)

    assert guest.log.warned == []
    assert not any(_is_write_of(SKEL_BASHRC_PATH)(request) for request in guest.access.requests())
