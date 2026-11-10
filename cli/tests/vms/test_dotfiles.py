"""The dotfiles sync both accounts share: a failed sync skips the install, and the install runs on its own."""

from __future__ import annotations

from agentworks.execution.errors import CommandFailed
from agentworks.execution.requests import Command
from agentworks.execution.results import Completed, Result
from agentworks.vms.initializer.dotfiles import sync_dotfiles
from tests.phase_b_support import StepGuest

HOME = "/home/agentworks"
DOTFILES = f"{HOME}/.dotfiles"
DOTFILES_URL = "https://example.com/dots.git"


def _sync(guest: StepGuest) -> None:
    sync_dotfiles(
        guest.files,
        source=f"git::{DOTFILES_URL}",
        destination="~/.dotfiles",
        install_cmd="./install.sh",
        shell="zsh",
        home=HOME,
        log=guest.log,
    )


def test_a_failed_dotfiles_sync_skips_the_install_and_says_the_destination_is_unchanged() -> None:
    guest = StepGuest(local_files=False)
    guest.respond(Command(("test", "-d", f"{DOTFILES}/.git"), deadline=60), Result(Completed(1)))
    guest.respond(Command(("test", "-d", DOTFILES), deadline=60), Result(Completed(1)))
    guest.respond(
        Command(("git", "clone", "--", DOTFILES_URL, DOTFILES), deadline=120),
        CommandFailed("box", 128, Result(Completed(128), stderr=b"fatal: repository not found\n"), streams_logged=True),
    )

    _sync(guest)

    (warning,) = guest.log.warned
    assert DOTFILES in warning and "repository not found" in warning
    assert not any(isinstance(request, Command) and request.argv[0] == "zsh" for request in guest.commands())


def test_a_failed_dotfiles_install_is_reported_on_its_own_and_runs_in_a_login_shell_in_the_checkout() -> None:
    guest = StepGuest(local_files=False)
    guest.respond(
        Command(("git", "-C", DOTFILES, "config", "--get", "remote.origin.url"), deadline=60, log_streams=False),
        Result(Completed(0), f"{DOTFILES_URL}\n".encode()),
    )
    install = Command(("zsh", "-lc", "./install.sh"), cwd=DOTFILES, deadline=120)
    guest.respond(install, CommandFailed("box", 1, Result(Completed(1)), streams_logged=True))

    _sync(guest)

    assert guest.commands()[-1] == install
    assert len(guest.log.warned) == 1
