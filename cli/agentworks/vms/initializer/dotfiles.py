"""Dotfiles: sync an account's checkout, then run its install, for the admin and each agent alike."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks import output
from agentworks.execution.requests import Command
from agentworks.sources import DOTFILES_DEADLINE, expand_destination, fetch_dir, parse_source_ref

from .steps import STEP_FAILURES, warn_step

if TYPE_CHECKING:
    from agentworks.execution.files import Files

    from .steps import InitLog


def sync_dotfiles(
    files: Files, *, source: str, destination: str, install_cmd: str, shell: str, home: str, log: InitLog
) -> None:
    """Sync the dotfiles ``source`` names to ``destination``, then run ``install_cmd`` in the checkout.

    Everything runs as the account ``files`` acts as, whose home is ``home``.
    The sync is ``sources.fetch_dir``. The install runs as ``{shell} -lc``
    with the checkout as its working directory, so it sees the account's
    login environment (its profile fragment, written earlier) and no
    operator environment. Sync and install warn separately, because a failed
    sync skips the install entirely; saying so is the difference between a
    warning an operator can act on and a later failure that looks unrelated
    to it.
    """
    log.step("Dotfiles")
    dest = destination
    try:
        dest = expand_destination(destination, home)
        ref = parse_source_ref(source)
        output.info(f"Syncing dotfiles from {source}...")
        fetch_dir(ref, files, dest, log=log)
    except STEP_FAILURES as error:
        summary = f"dotfiles sync failed, so '{install_cmd}' did not run and {dest} still holds its previous contents"
        warn_step(log, summary, error)
        return
    output.info(f"Running dotfiles install: {install_cmd}")
    try:
        files.access.run(Command((shell, "-lc", install_cmd), cwd=dest, deadline=DOTFILES_DEADLINE), check=True)
    except STEP_FAILURES as error:
        warn_step(log, f"dotfiles install '{install_cmd}' failed in {dest}", error)
