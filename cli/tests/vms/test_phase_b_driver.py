"""Phase B's VM Initialization section, driven whole over a recording admin access.

Every request the section makes runs on the access, and its file operations
are answered from memory (nothing touches this machine's paths). The Admin
Initialization steps that still run on the legacy transport are stubbed.
What run_initialization makes of a warning or a raise (partial or failed) is
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
from agentworks.execution.requests import Command, PythonProgram
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

    from agentworks.db import Database
    from agentworks.execution.requests import RunRequest
    from tests.vm_access_support import Call

HOME = "/home/agentworks"


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


class Guest(StepGuest):
    """A steady guest: reads find a hardened ``/etc/fstab`` and empty files, everything else succeeds."""

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
        return None


@pytest.fixture
def run_section(db: Database, monkeypatch: pytest.MonkeyPatch) -> Callable[[Guest], None]:
    db.insert_vm("box", site="lima-local", hostname="box")
    monkeypatch.setattr("agentworks.vms.sites.site_platform_name", lambda site, registry: "lima")
    # Admin Initialization steps still on the legacy transport.
    for name in (
        "_harden_admin_home",
        "configure_user_git_credentials",
        "_write_mise_config",
        "_run_mise_install",
        "_write_agentworks_profile",
        "_write_agentworks_rc",
        "_ensure_agentworks_files_sourced",
    ):
        monkeypatch.setattr(driver, name, lambda *args, **kwargs: None)
    kinds: dict[str, dict[str, Any]] = {
        "system-install-command": {
            "sys-tool": SystemInstallCommandEntry(name="sys-tool", description="", command="install-sys-tool")
        },
        "user-install-command": {
            "user-tool": UserInstallCommandEntry(name="user-tool", description="", command="install-user-tool")
        },
    }
    registry: Any = MagicMock()
    registry.iter_kind_items.side_effect = lambda kind: iter(kinds.get(kind, {}).items())
    config: Any = MagicMock()
    config.paths.vm_workspaces = "/opt/agentworks/workspaces"

    def run(guest: Guest) -> None:
        log: Any = guest.log
        driver._phase_b_setup(
            db,
            config,
            registry,
            ResolvedVMTemplate(name="default", apt=["jq"], system_install_commands=["sys-tool"]),
            AdminConfig(user_install_commands=["user-tool"]),
            "box",
            guest.files,
            (),
            HOME,
            "agentworks",
            log,
            legacy_target=MagicMock(),
            debian_release=DebianRelease.TRIXIE,
            operation=driver.VMInitializationOperation.VM_REINIT,
        )

    return run


def test_a_steady_section_runs_every_step_without_a_warning(run_section: Callable[[Guest], None]) -> None:
    guest = Guest()

    run_section(guest)

    assert guest.log.warned == []
    # The section's last step, the agent socket root, ran (on this guest's empty probe it is rebuilt).
    assert Command(("chmod", "2771", "--", AGENT_SOCKET_ROOT), elevated=True, deadline=60) in guest.commands()


def test_skel_seeds_and_the_zprofile_block_are_written_after_apt_installs(
    run_section: Callable[[Guest], None],
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


def test_no_request_carries_an_environment_but_apts(run_section: Callable[[Guest], None]) -> None:
    """Provisioning is hermetic: identity reaches installs only through the profile fragments."""
    guest = Guest()

    run_section(guest)

    requests = guest.access.requests()
    assert install_run("bash", "install-sys-tool") in requests
    assert install_run("bash", "install-user-tool") in requests
    environments = {json.dumps(dict(request.env), sort_keys=True) for request in requests}
    assert environments <= {"{}", json.dumps({"DEBIAN_FRONTEND": "noninteractive"})}


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
    run_section: Callable[[Guest], None], error: BaseException
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
    run_section: Callable[[Guest], None], stop: Callable[[Guest], None], raised: type[BaseException]
) -> None:
    guest = Guest()
    stop(guest)

    with pytest.raises(raised):
        run_section(guest)

    assert guest.log.warned == []
    assert not any(_is_write_of(SKEL_BASHRC_PATH)(request) for request in guest.access.requests())


# -- Dotfiles ---------------------------------------------------------------------

DOTFILES = "/home/agentworks/.dotfiles"
DOTFILES_URL = "https://example.com/dots.git"


def _dotfiles_admin() -> AdminConfig:
    return AdminConfig(dotfiles_source=f"git::{DOTFILES_URL}", dotfiles_install_cmd="./install.sh")


def test_a_failed_dotfiles_sync_skips_the_install_and_says_the_destination_is_unchanged() -> None:
    guest = StepGuest(local_files=False)
    guest.respond(Command(("test", "-d", f"{DOTFILES}/.git"), deadline=60), Result(Completed(1)))
    guest.respond(Command(("test", "-d", DOTFILES), deadline=60), Result(Completed(1)))
    guest.respond(
        Command(("git", "clone", DOTFILES_URL, DOTFILES), deadline=120),
        CommandFailed("box", 128, Result(Completed(128), stderr=b"fatal: repository not found\n"), streams_logged=True),
    )

    driver._sync_dotfiles(guest.files, _dotfiles_admin(), "zsh", HOME, guest.log)

    (warning,) = guest.log.warned
    assert DOTFILES in warning and "repository not found" in warning
    assert not any(isinstance(request, Command) and request.argv[0] == "zsh" for request in guest.commands())


def test_a_failed_dotfiles_install_is_reported_on_its_own_and_runs_in_a_login_shell_in_the_checkout() -> None:
    guest = StepGuest(local_files=False)
    guest.respond(
        Command(("git", "-C", DOTFILES, "remote", "get-url", "origin"), deadline=60),
        Result(Completed(0), f"{DOTFILES_URL}\n".encode()),
    )
    install = Command(("zsh", "-lc", "./install.sh"), cwd=DOTFILES, deadline=120)
    guest.respond(install, CommandFailed("box", 1, Result(Completed(1)), streams_logged=True))

    driver._sync_dotfiles(guest.files, _dotfiles_admin(), "zsh", HOME, guest.log)

    assert guest.commands()[-1] == install
    assert len(guest.log.warned) == 1
